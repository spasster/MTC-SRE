"""Сравнение восстановления в одинаковом локальном контуре k3d."""

import argparse
import base64
import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PROMQL = {
    "p95": 'histogram_quantile(0.95,sum by(le)(rate(http_request_duration_seconds_bucket{namespace="demo",job="checkout",path="/work"}[1m])))',
    "request_rate": 'sum(rate(http_requests_total{namespace="demo",job="checkout",path="/work"}[1m]))',
    "error_ratio": '(sum(rate(http_requests_total{namespace="demo",job="checkout",path="/work",status=~"5.."}[1m])) or vector(0)) / sum(rate(http_requests_total{namespace="demo",job="checkout",path="/work"}[1m]))',
    "synthetic_success": 'min(probe_success{service="checkout",target_namespace="demo"})',
    "synthetic_time": 'min(timestamp(probe_success{service="checkout",target_namespace="demo"}))',
    "alerts_firing": 'count(ALERTS{service="checkout",alertstate="firing"}) or vector(0)',
    "alerts_active": 'count(ALERTS{service="checkout"}) or vector(0)',
    "cpu_throttle": 'max(rate(container_cpu_cfs_throttled_periods_total{namespace="demo",container="checkout"}[1m]) / rate(container_cpu_cfs_periods_total{namespace="demo",container="checkout"}[1m]))',
}


def cmd(*args):
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError("Команда не выполнена: " + " ".join(args) + "\n" + result.stderr[-1500:])
    return result.stdout


def api(url, params=None, token=None):
    request = Request(url + ("?" + urlencode(params) if params else ""))
    if token:
        request.add_header("Authorization", "Bearer " + token)
    with urlopen(request, timeout=10) as response:
        return json.load(response)


def deployment(name, namespace):
    return json.loads(cmd("kubectl", "get", "deployment", name, "-n", namespace, "-o", "json"))


def scalar(base, query):
    rows = api(base + "/api/v1/query", {"query": query}).get("data", {}).get("result", [])
    if not rows:
        return None
    value = float(rows[0]["value"][1])
    return value if math.isfinite(value) else None


def metrics(base):
    sample = {key: scalar(base, query) for key, query in PROMQL.items()}
    obj = deployment("checkout", "demo")
    desired = obj["spec"]["replicas"]
    status = obj.get("status", {})
    sample.update(
        time=time.time(), replicas=desired,
        rollout_ready=(status.get("observedGeneration", 0) >= obj["metadata"]["generation"]
                       and status.get("updatedReplicas", 0) == desired
                       and status.get("availableReplicas", 0) >= desired
                       and status.get("replicas", 0) == desired),
    )
    return sample


def healthy(sample):
    return (
        sample["p95"] is not None and sample["p95"] < 0.3
        and sample["error_ratio"] is not None and sample["error_ratio"] <= 0.005
        and sample["request_rate"] is not None and sample["request_rate"] > 0
        and sample["synthetic_success"] == 1 and sample["synthetic_time"] is not None
        and 0 <= sample["time"] - sample["synthetic_time"] <= 45 and sample["rollout_ready"]
    )


def wait_healthy(base, timeout=180):
    deadline, streak = time.time() + timeout, 0
    while time.time() < deadline:
        sample = metrics(base)
        if healthy(sample) and sample["alerts_active"] == 0:
            streak += 1
            if streak >= 3:
                return
        else:
            streak = 0
        time.sleep(5)
    raise RuntimeError("Начальный SLO не восстановлен; эксперимент не начат")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default=os.getenv("CLUSTER", "mtc"))
    parser.add_argument("--scenario", choices=["slow-pod", "spike"], default="slow-pod")
    parser.add_argument("--mode", choices=["baseline", "auto"], required=True)
    parser.add_argument("--manual-delay", type=int, default=30)
    parser.add_argument("--timeout", type=int, default=420)
    parser.add_argument("--prometheus", default="http://127.0.0.1:19090")
    parser.add_argument("--output", required=True)
    parser.add_argument("--natural-end", action="store_true", help="Дождаться окончания spike и возврата масштаба самим healer")
    parser.add_argument("--incident-chain-output", help="Проверить exemplar, трейс и лог медленного запроса в этом инциденте")
    parser.add_argument("--spike-duration", type=int, help="Длительность дополнительной нагрузки в секундах для короткой демонстрации")
    args = parser.parse_args()
    if args.natural_end and (args.scenario != "spike" or args.mode != "auto"):
        parser.error("--natural-end допустим только для auto spike")
    if args.spike_duration is not None and (args.scenario != "spike" or args.spike_duration < 120):
        parser.error("--spike-duration допустим для spike и должен быть не менее 120 с")
    os.environ["CLUSTER"] = args.cluster
    cmd("sh", "scripts/require-context.sh", args.cluster)
    existing = json.loads(cmd("kubectl", "get", "networkchaos,podchaos", "-n", "demo", "-o", "json"))
    if existing["items"]:
        raise SystemExit("В demo уже есть эксперимент Chaos Mesh. Сначала завершить его.")
    jobs = json.loads(cmd("kubectl", "get", "jobs", "-n", "demo", "-o", "json"))["items"]
    if any(item["metadata"]["name"] == "k6-spike" for item in jobs):
        raise SystemExit("В demo уже есть k6-spike. Сначала завершить его.")
    original_healer = deployment("healer", "heal")["spec"]["replicas"]
    original_checkout = deployment("checkout", "demo")["spec"]["replicas"]
    if original_checkout != 2:
        raise SystemExit("Для сравнения требуется исходное состояние с двумя репликами checkout")
    token = None
    forward = None
    report = {"scenario": args.scenario, "mode": args.mode, "manual_delay_seconds": args.manual_delay,
              "created_at": datetime.now(timezone.utc).isoformat(), "samples": [], "passed": False,
              "baseline_note": "Задержка оператора задаётся явно; это контролируемый baseline, не измерение работы человека."}
    try:
        if args.mode == "auto":
            # Временный импорт может исчезнуть после удаления всех подов baseline.
            # Исходный образ остается в Docker; подготовка заканчивается до внесения сбоя.
            report["healer_image_id"] = cmd("docker", "image", "inspect", "healer:dev", "--format", "{{.Id}}").strip()
            cmd("k3d", "image", "import", "healer:dev", "-c", args.cluster, "--mode", "direct")
        cmd("kubectl", "scale", "deployment/healer", "-n", "heal", "--replicas=" + ("0" if args.mode == "baseline" else "1"))
        if args.mode == "auto":
            cmd("kubectl", "rollout", "restart", "deployment/healer", "-n", "heal")
            cmd("kubectl", "rollout", "status", "deployment/healer", "-n", "heal", "--timeout=180s")
            secret = json.loads(cmd("kubectl", "get", "secret", "healer-webhook", "-n", "heal", "-o", "json"))
            token = base64.b64decode(secret["data"]["token"]).decode()
            forward = subprocess.Popen(["kubectl", "port-forward", "-n", "heal", "svc/healer", "18081:8080"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_healthy(args.prometheus)
        selected = json.loads(cmd("kubectl", "get", "pods", "-n", "demo", "-l", "app=checkout", "-o", "json"))["items"][0]["metadata"]["name"]
        if args.spike_duration:
            manifest = json.loads(cmd("kubectl", "create", "-f", "k8s/k6-spike.yaml", "--dry-run=client", "-o", "json"))
            manifest["spec"]["template"]["spec"]["containers"][0]["env"].append(
                {"name": "DURATION", "value": str(args.spike_duration) + "s"})
            subprocess.run(["kubectl", "apply", "-f", "-"], input=json.dumps(manifest), text=True, capture_output=True, check=True)
            report["spike_duration_seconds"] = args.spike_duration
        else:
            cmd("sh", "chaos/inject.sh", args.scenario)
        report["injected_at"] = time.time()
        report["target_pod"] = selected if args.scenario == "slow-pod" else None
        deadline, streak = time.time() + args.timeout, 0
        while time.time() < deadline:
            sample = metrics(args.prometheus)
            report["samples"].append(sample)
            if not healthy(sample) and report.get("detected_at") is None:
                report["detected_at"] = sample["time"]
            if sample["alerts_firing"] and report.get("alert_firing_at") is None:
                report["alert_firing_at"] = sample["time"]
            if args.incident_chain_output and not report.get("incident_chain_verified") and sample["p95"] and sample["p95"] > .3:
                result = subprocess.run([sys.executable, "scripts/verify-observability.py", "--since", str(report["injected_at"]),
                                         "--min-duration", ".3", "--output", args.incident_chain_output], capture_output=True)
                report["incident_chain_verified"] = result.returncode == 0
            if args.mode == "baseline" and report.get("alert_firing_at") and not report.get("api_accepted_at"):
                if sample["time"] >= report["alert_firing_at"] + args.manual_delay:
                    if args.scenario == "slow-pod":
                        cmd("kubectl", "delete", "pod", selected, "-n", "demo", "--wait=false")
                    else:
                        cmd("kubectl", "scale", "deployment/checkout", "-n", "demo", "--replicas=5")
                    report["api_accepted_at"] = time.time()
            elif args.mode == "auto":
                status = api("http://127.0.0.1:18081/status", token=token)
                timing = status if (status.get("api_accepted_at") or 0) >= report["injected_at"] else (status.get("last_recovery") or {})
                accepted = timing.get("api_accepted_at")
                if accepted and accepted >= report["injected_at"]:
                    report["api_accepted_at"] = accepted
                restored = timing.get("slo_restored_at")
                if restored and restored >= report["injected_at"]:
                    report["healer_slo_restored_at"] = restored
            print(f"После внесения сбоя {sample['time'] - report['injected_at']:.0f} с: "
                  f"p95={sample['p95']}, доля ошибок={sample['error_ratio']}, реплик={sample['replicas']}", flush=True)
            if report.get("api_accepted_at") and healthy(sample):
                if args.natural_end and report.get("slo_restored_at"):
                    job = json.loads(cmd("kubectl", "get", "job", "k6-spike", "-n", "demo", "-o", "json"))
                    if job.get("status", {}).get("failed"):
                        raise RuntimeError("Spike завершился ошибкой")
                    if job.get("status", {}).get("succeeded") and sample["replicas"] == 2:
                        report.update(natural_job_completion=True, autonomous_scale_down=True, scale_down_at=sample["time"], passed=True)
                        break
                    time.sleep(5)
                    continue
                if args.scenario == "spike":
                    job = json.loads(cmd("kubectl", "get", "job", "k6-spike", "-n", "demo", "-o", "json"))
                    if job.get("status", {}).get("succeeded"):
                        report["error"] = "SLO восстановился после завершения нагрузки; результат scale не подтверждён"
                        break
                streak += 1
                if streak >= 3:
                    report["slo_restored_at"] = sample["time"]
                    report["mttr_seconds"] = sample["time"] - report["injected_at"]
                    report["detection_seconds"] = report["detected_at"] - report["injected_at"]
                    report["api_acceptance_seconds"] = report["api_accepted_at"] - report["injected_at"]
                    report["passed"] = not args.natural_end
                    if not args.natural_end:
                        break
            else:
                streak = 0
            time.sleep(5)
        if not report["passed"]:
            report.setdefault("error", "Восстановление и требуемый возврат масштаба не подтверждены до таймаута")
        if args.incident_chain_output and not report.get("incident_chain_verified"):
            report.update(passed=False, error="Связка метрика -> трейс -> лог на инциденте не подтверждена")
    except Exception as exc:
        report["error"] = str(exc)
    except KeyboardInterrupt:
        report["error"] = "Эксперимент остановлен оператором; восстановление не подтверждено"
    finally:
        if forward is not None:
            forward.terminate()
            forward.wait(timeout=10)
        if args.scenario == "slow-pod":
            cmd("kubectl", "delete", "networkchaos", "checkout-slow-pod", "-n", "demo", "--ignore-not-found")
        else:
            try:
                pods = json.loads(cmd("kubectl", "get", "pods", "-n", "demo", "-l", "job-name=k6-spike", "-o", "json"))["items"]
                if pods:
                    pod = pods[0]["metadata"]["name"]
                    subprocess.run(["kubectl", "exec", "-n", "demo", pod, "-c", "k6", "--", "sh", "-c", "kill -INT 1"],
                                   capture_output=True, timeout=10)
                    for _ in range(10):
                        logs = cmd("kubectl", "logs", "-n", "demo", pod, "-c", "k6")
                        for line in logs.splitlines():
                            if line.startswith("MTC_SUMMARY "):
                                report["k6_summary"] = json.loads(line[len("MTC_SUMMARY "):])
                        if "k6_summary" in report:
                            break
                        time.sleep(2)
            except Exception:
                report["k6_summary_error"] = "Итог клиента k6 не получен"
            cmd("kubectl", "delete", "job", "k6-spike", "-n", "demo", "--ignore-not-found")
        cmd("kubectl", "scale", "deployment/checkout", "-n", "demo", "--replicas=" + str(original_checkout))
        cmd("kubectl", "scale", "deployment/healer", "-n", "heal", "--replicas=" + str(original_healer))
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    if not report["passed"]:
        raise SystemExit(report.get("error", "Эксперимент не прошёл"))
    print(f"Восстановление подтверждено, MTTR {report['mttr_seconds']:.1f} с. Результат: {args.output}")


if __name__ == "__main__":
    main()
