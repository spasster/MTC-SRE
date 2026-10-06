"""Проверить удержание SLO в заранее фиксированной области ограниченного хаоса."""

import argparse
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from experiment import cmd, deployment, metrics, scalar, wait_healthy, wait_incident_closed

PROM = "http://127.0.0.1:19090"
KINDS = {"pod": "PodChaos", "network": "NetworkChaos", "cpu": "StressChaos"}


def slo_held(sample):
    return (sample.get("p95") is not None and 0 <= sample["p95"] < .3
            and sample.get("error_ratio") is not None and 0 <= sample["error_ratio"] <= .005
            and sample.get("request_rate") is not None and sample["request_rate"] > 0
            and sample.get("synthetic_success") == 1 and sample.get("synthetic_time") is not None
            and 0 <= sample["time"] - sample["synthetic_time"] <= 45)


def fault_manifest(fault, pod):
    spec = {"mode": "one", "selector": {"namespaces": ["demo"], "pods": {"demo": [pod]}}}
    if fault == "pod":
        spec["action"] = "pod-kill"
    elif fault == "network":
        spec.update(action="delay", direction="to", duration="120s", delay={"latency": "20ms"},
                    target={"mode": "all", "selector": {"namespaces": ["demo"], "labelSelectors": {"app": "payment"}}})
    else:
        spec.update(duration="120s", containerNames=["checkout"], stressors={"cpu": {"workers": 1, "load": 5}})
    return {"apiVersion": "chaos-mesh.org/v1alpha1", "kind": KINDS[fault],
            "metadata": {"name": "checkout-limited-" + fault, "namespace": "demo"}, "spec": spec}


def apply(manifest):
    result = subprocess.run(["kubectl", "create", "-f", "-"], input=json.dumps(manifest), capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("Тестовый объект не создан: " + result.stderr[-500:])


def client_passed(summary):
    return (summary.get("http_reqs", {}).get("count", 0) >= 299
            and summary.get("dropped_iterations", {}).get("count", 0) == 0
            and summary.get("http_req_failed", {}).get("rate", 1) <= .005
            and summary.get("checks", {}).get("rate", 0) >= .999
            and 0 <= summary.get("http_req_duration", {}).get("p(95)", float("inf")) < 300)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default=os.getenv("CLUSTER", "mtc"))
    parser.add_argument("--output", default="docs/results/limited-chaos.json")
    args = parser.parse_args()
    os.environ["CLUSTER"] = args.cluster
    cmd("sh", "scripts/require-context.sh", args.cluster)
    if json.loads(cmd("kubectl", "get", "networkchaos,podchaos,stresschaos", "-n", "demo", "-o", "json"))["items"]:
        raise SystemExit("В demo уже есть хаос; проверка не начата")
    jobs = json.loads(cmd("kubectl", "get", "jobs", "-n", "demo", "-o", "json"))["items"]
    if any(row["metadata"]["name"] == "k6-spike" or row["metadata"]["name"].startswith("k6-slo-") for row in jobs):
        raise SystemExit("В demo уже есть нагрузочный опыт; проверка не начата")
    if deployment("checkout", "demo")["spec"]["replicas"] != 2 or deployment("healer", "heal")["spec"]["replicas"] != 1:
        raise SystemExit("Для проверки нужны две реплики checkout и один healer")
    wait_healthy(PROM, timeout=300)
    initial = wait_incident_closed()
    original_background = deployment("k6-background", "demo")["spec"]["replicas"]
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "passed": False, "cases": [],
              "source_commit": cmd("git", "rev-parse", "HEAD").strip(), "initial_incident": initial,
              "source_worktree_dirty": bool(cmd("git", "status", "--porcelain")),
              "profile": {"client_rps": 2, "duration_per_case_seconds": 150, "warmup_seconds": 30,
                          "replicas": 2, "network_delay_ms": 20, "cpu_workers": 1, "cpu_load_percent_of_core": 5},
              "resources_per_replica": deployment("checkout", "demo")["spec"]["template"]["spec"]["containers"][0]["resources"],
              "limitation": "Доказательство относится к указанной области отказа и короткому окну, не ко всем интенсивностям хаоса или месячному SLO. Полный сетевой разрыв и перегрузка сохраняют отрицательные результаты."}
    owned = []
    try:
        cmd("kubectl", "scale", "deployment/k6-background", "-n", "demo", "--replicas=0")
        for fault in KINDS:
            name = "k6-slo-" + fault
            manifest = json.loads(cmd("kubectl", "create", "-f", "k8s/k6-spike.yaml", "--dry-run=client", "-o", "json"))
            manifest["metadata"]["name"] = name
            manifest["spec"]["template"]["spec"]["containers"][0]["args"] = ["run", "/scripts/limited.js"]
            apply(manifest)
            owned.append(("job", name))
            case = {"fault": fault, "samples": [], "passed": False}
            report["cases"].append(case)
            deadline = time.time() + 90
            while time.time() < deadline:
                pods = json.loads(cmd("kubectl", "get", "pods", "-n", "demo", "-l", "job-name=" + name, "-o", "json"))["items"]
                running = [row for row in pods if any(status.get("name") == "k6" and status.get("state", {}).get("running")
                           for status in row.get("status", {}).get("containerStatuses", []))]
                if running:
                    break
                time.sleep(3)
            else:
                raise RuntimeError("Клиент k6 не запущен")
            case["client_started_at"] = time.time()
            warmup_end = time.time() + 30
            while time.time() < warmup_end:
                time.sleep(5)
            pods = json.loads(cmd("kubectl", "get", "pods", "-n", "demo", "-l", "app=checkout", "-o", "json"))["items"]
            pod = next(row for row in pods if not row["metadata"].get("deletionTimestamp") and row["status"]["phase"] == "Running")
            case.update(target_pod=pod["metadata"]["name"], target_uid=pod["metadata"]["uid"], injected_at=time.time())
            chaos = fault_manifest(fault, case["target_pod"])
            case["fault_spec"] = chaos["spec"]
            apply(chaos)
            owned.append((KINDS[fault].lower(), chaos["metadata"]["name"]))
            deadline = time.time() + 180
            while time.time() < deadline:
                sample = metrics(PROM)
                sample["chaos_alert_firing"] = scalar(PROM, 'count(ALERTS{alertname="ChaosFaultObserved",fault="' + fault + '",alertstate="firing"}) or vector(0)')
                sample["checkout_cpu_cores"] = scalar(PROM, 'sum(rate(container_cpu_usage_seconds_total{namespace="demo",container="checkout"}[1m]))')
                case["samples"].append(sample)
                obj = json.loads(cmd("kubectl", "get", KINDS[fault].lower(), chaos["metadata"]["name"], "-n", "demo", "-o", "json"))
                conditions = obj.get("status", {}).get("conditions", [])
                if any(row.get("type") == "AllInjected" and row.get("status") == "True" for row in conditions):
                    case["injection_confirmed"] = True
                    case["chaos_conditions"] = conditions
                job = json.loads(cmd("kubectl", "get", "job", name, "-n", "demo", "-o", "json"))
                if job.get("status", {}).get("succeeded") or job.get("status", {}).get("failed"):
                    break
                time.sleep(5)
            logs = cmd("kubectl", "logs", "job/" + name, "-n", "demo", "-c", "k6")
            summaries = [json.loads(line[len("MTC_SUMMARY "):]) for line in logs.splitlines() if line.startswith("MTC_SUMMARY ")]
            if not summaries:
                raise RuntimeError("Итог клиента не получен")
            case["client"] = summaries[-1]
            case["alert_observed"] = any(sample["chaos_alert_firing"] for sample in case["samples"])
            case["slo_held_in_all_samples"] = bool(case["samples"]) and all(slo_held(sample) for sample in case["samples"])
            current = json.loads(cmd("kubectl", "get", "pods", "-n", "demo", "-l", "app=checkout", "-o", "json"))["items"]
            case["pod_replaced"] = not any(row["metadata"]["uid"] == case["target_uid"] for row in current)
            case["passed"] = (not job.get("status", {}).get("failed") and case.get("injection_confirmed") is True
                              and case["alert_observed"] and case["slo_held_in_all_samples"]
                              and client_passed(case["client"]) and (fault != "pod" or case["pod_replaced"])
                              and case["samples"][-1]["rollout_ready"] and all(sample["replicas"] == 2 for sample in case["samples"]))
            for kind, object_name in list(owned):
                cmd("kubectl", "delete", kind, object_name, "-n", "demo", "--ignore-not-found", "--timeout=60s")
                owned.remove((kind, object_name))
            print(f"Ограниченный отказ {fault}: SLO={case['slo_held_in_all_samples']}, алерт={case['alert_observed']}, результат={case['passed']}", flush=True)
            if not case["passed"]:
                raise RuntimeError("Удержание SLO не подтверждено: " + fault)
        report["passed"] = True
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        for kind, object_name in owned:
            cmd("kubectl", "delete", kind, object_name, "-n", "demo", "--ignore-not-found", "--timeout=60s")
        cmd("kubectl", "scale", "deployment/k6-background", "-n", "demo", "--replicas=" + str(original_background))
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    if not report["passed"]:
        raise SystemExit(report.get("error", "Удержание SLO не подтверждено"))


if __name__ == "__main__":
    main()
