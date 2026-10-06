import os
import math

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
    if whole is None or whole <= 0:
        return None
    return part / whole


def gather(namespace, actuator=None, query=None):
    actor = actuator or K8sActuator()
    ask = query or query_prometheus
    try:
        p95 = _by_label(
            ask(
                "histogram_quantile(0.95, sum by (le, pod) "
                '(rate(http_request_duration_seconds_bucket{job="checkout",path="/work"}[1m])))'
            ),
            "pod",
        )
        p95_recent = _by_label(
            ask(
                "histogram_quantile(0.95, sum by (le, pod) "
                '(rate(http_request_duration_seconds_bucket{job="checkout",path="/work"}[30s])))'
            ),
            "pod",
        )
        p95_all = _scalar(
            ask(
                "histogram_quantile(0.95, sum by (le) "
                '(rate(http_request_duration_seconds_bucket{job="checkout",path="/work"}[1m])))'
            )
        )
        throttle = _by_label(
            ask(
                "sum by (pod) (rate(container_cpu_cfs_throttled_periods_total"
                f'{{namespace="{namespace}",container="checkout"}}[1m])) / '
                "sum by (pod) (rate(container_cpu_cfs_periods_total"
                f'{{namespace="{namespace}",container="checkout"}}[1m]))'
            ),
            "pod",
        )
        requests = _scalar(
            ask('sum(rate(http_requests_total{job="checkout",path="/work"}[1m]))')
        )
        errors = _scalar(
            ask(
                'sum(rate(http_requests_total{job="checkout",path="/work",status=~"5.."}[1m]))'
            )
        )
        redis_errors = _scalar(
            ask(
                'sum(rate(dependency_errors_total{job="checkout",dependency="redis"}[1m]))'
            )
        )
        payment_timeouts = _scalar(
            ask(
                'sum(rate(dependency_errors_total{job="checkout",dependency="payment",reason="timeout"}[1m]))'
            )
        )
        revision_age = actor.revision_age_seconds(namespace, "checkout")
        replicas = actor.replicas(namespace, "checkout")
        redis_ready = actor.ready(namespace, "redis")
        payment_ready = actor.ready(namespace, "payment")
    except Exception as exc:
        return {"error": str(exc)}
    pods = []
    for name, latency in p95.items():
        pods.append({
            "pod": name,
            "p95": latency,
            "p95_recent": p95_recent.get(name),
            "throttle": throttle.get(name, 0.0),
        })
    return {
        "replicas": replicas,
        "request_rate": requests,
        "p95": p95_all,
        "revision_age_seconds": revision_age,
        "error_ratio": _ratio(errors or 0.0, requests),
        "redis_error_ratio": _ratio(redis_errors or 0.0, requests),
        "payment_timeout_ratio": _ratio(payment_timeouts or 0.0, requests),
        "redis_ready": redis_ready,
        "payment_ready": payment_ready,
        "pods": pods,
    }
