import os
import json
import math
import re

import httpx

from app.k8s import K8sActuator


class EvidenceError(Exception):
    pass


def prometheus_url():
    return os.getenv(
        "PROMETHEUS_URL",
        "http://kube-prom-kube-prometheus-prometheus.obs.svc:9090",
    ).rstrip("/")


def parse_vector(payload):
    rows = []
    for item in payload:
        raw = item.get("value", [None, None])[1]
        if raw in (None, "NaN", "+Inf", "-Inf"):
            continue
        number = float(raw)
        if math.isfinite(number):
            rows.append((item.get("metric") or {}, number))
    return rows


def query_prometheus(promql, base_url=None, client=None):
    url = (base_url or prometheus_url()) + "/api/v1/query"
    http = client or httpx.Client(timeout=5)
    own_client = client is None
    try:
        response = http.get(url, params={"query": promql})
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise EvidenceError(str(exc)) from exc
    finally:
        if own_client:
            http.close()
    if body.get("status") != "success":
        raise EvidenceError(body.get("error") or "prometheus status")
    return parse_vector(body.get("data", {}).get("result") or [])


def _scalar(rows):
    if not rows:
        return None
    return rows[0][1]


def _by_label(rows, label):
    found = {}
    for metric, value in rows:
        name = metric.get(label)
        if name:
            found[name] = value
    return found


def _ratio(part, whole):
    if part is None or whole is None or whole <= 0:
        return None
    return part / whole


def gather(namespace, actuator=None, query=None):
    actor = actuator or K8sActuator()
    ask = query or query_prometheus
    try:
        current = actor.telemetry_pods(namespace, "checkout")
        if not current:
            raise EvidenceError("Нет рабочих подов текущей ревизии checkout")
        selector = (f'namespace={json.dumps(namespace)},job="checkout",'
                    f'pod=~{json.dumps("|".join(re.escape(name) for name in sorted(current)))}')

        def per_pod(expression):
            values = _by_label(ask(expression), "pod")
            if set(values) != set(current):
                raise EvidenceError("Неполные метрики текущих подов checkout")
            return values

        up = per_pod(f'up{{{selector},endpoint="metrics"}}')
        ages = per_pod(f'max by(pod)(time() - timestamp(http_requests_total{{{selector},path="/work"}}))')
        if any(value != 1 for value in up.values()) or any(not 0 <= age <= 45 for age in ages.values()):
            raise EvidenceError("Метрики checkout недоступны или устарели")
        p95 = _by_label(
            ask(
                "histogram_quantile(0.95, sum by (le, pod) "
                f'(rate(http_request_duration_seconds_bucket{{{selector},path="/work"}}[1m])))'
            ),
            "pod",
        )
        p95_recent = _by_label(
            ask(
                "histogram_quantile(0.95, sum by (le, pod) "
                f'(rate(http_request_duration_seconds_bucket{{{selector},path="/work"}}[30s])))'
            ),
            "pod",
        )
        p95_all = _scalar(
            ask(
                "histogram_quantile(0.95, sum by (le) "
                f'(rate(http_request_duration_seconds_bucket{{{selector},path="/work"}}[1m])))'
            )
        )
        throttle = _by_label(
            ask(
                "sum by (pod) (rate(container_cpu_cfs_throttled_periods_total"
                f'{{namespace={json.dumps(namespace)},container="checkout",pod=~{json.dumps("|".join(re.escape(name) for name in sorted(current)))}}}[1m])) / '
                "sum by (pod) (rate(container_cpu_cfs_periods_total"
                f'{{namespace={json.dumps(namespace)},container="checkout",pod=~{json.dumps("|".join(re.escape(name) for name in sorted(current)))}}}[1m]))'
            ),
            "pod",
        )
        def rate_sum(metric, labels=""):
            return sum(per_pod(f'sum by(pod)(rate({metric}{{{selector}{labels}}}[1m]))').values())

        requests = rate_sum("http_requests_total", ',path="/work"')
        errors = rate_sum("http_requests_total", ',path="/work",status=~"5.."')
        redis_errors = rate_sum("dependency_errors_total", ',dependency="redis"')
        payment_timeouts = rate_sum("dependency_errors_total", ',dependency="payment",reason="timeout"')
        payment_errors = rate_sum("dependency_errors_total", ',dependency="payment"')
        application_errors = rate_sum("application_errors_total")
        if set(p95) != set(current) or set(p95_recent) != set(current):
            raise EvidenceError("Не хватает измерений задержки текущих подов")
        probe = f'service="checkout",target_namespace={json.dumps(namespace)}'
        synthetic_success = _scalar(ask(f'min(probe_success{{{probe}}})'))
        synthetic_age = _scalar(ask(f'max(time() - timestamp(probe_success{{{probe}}}))'))
        revision_age = actor.revision_age_seconds(namespace, "checkout")
        replicas = actor.replicas(namespace, "checkout")
        redis_ready = actor.ready(namespace, "redis")
        payment_ready = actor.ready(namespace, "payment")
    except Exception as exc:
        return {"error": str(exc), "telemetry_complete": False, "request_rate": None,
                "error_ratio": None, "p95": None, "pods": []}
    pods = []
    for name, latency in p95.items():
        pods.append({
            "pod": name,
            "uid": current[name],
            "p95": latency,
            "p95_recent": p95_recent.get(name),
            "throttle": throttle.get(name),
        })
    return {
        "replicas": replicas,
        "request_rate": requests,
        "p95": p95_all,
        "revision_age_seconds": revision_age,
        "error_ratio": _ratio(errors, requests),
        "redis_error_ratio": _ratio(redis_errors, requests),
        "payment_timeout_ratio": _ratio(payment_timeouts, requests),
        "payment_error_ratio": _ratio(payment_errors, requests),
        "application_error_ratio": _ratio(application_errors, requests),
        "synthetic_success": synthetic_success,
        "synthetic_age_seconds": synthetic_age,
        "telemetry_complete": True,
        "telemetry_age_seconds": max(ages.values()),
        "redis_ready": redis_ready,
        "payment_ready": payment_ready,
        "pods": pods,
    }
