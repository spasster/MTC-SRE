"""Проверить удержание SLO в заранее фиксированной области ограниченного хаоса."""

import argparse
import json
import os
import subprocess
import time
import uuid
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


def fault_manifest(fault, pod, payment_pod=None):
    spec = {"mode": "one", "selector": {"namespaces": ["demo"], "pods": {"demo": [pod]}}}
    if fault == "pod":
        spec["action"] = "pod-kill"
    elif fault == "network":
        if not payment_pod:
            raise ValueError("Для разрыва нужен конкретный под payment")
        spec.update(action="partition", direction="to", duration="120s",
                    target={"mode": "one", "selector": {"namespaces": ["demo"], "pods": {"demo": [payment_pod]}}})
    else:
        spec.update(duration="120s", containerNames=["checkout"], stressors={"cpu": {"workers": 1, "load": 5}})
    return {"apiVersion": "chaos-mesh.org/v1alpha1", "kind": KINDS[fault],
            "metadata": {"name": "checkout-limited-" + fault, "namespace": "demo"}, "spec": spec}


def apply(manifest):
    result = subprocess.run(["kubectl", "create", "-f", "-"], input=json.dumps(manifest), capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("Тестовый объект не создан: " + result.stderr[-500:])


def client_passed(summary):
    return (summary.get("http_reqs", {}).get("count", 0) >= 359
            and summary.get("dropped_iterations", {}).get("count", 0) == 0
            and summary.get("http_req_failed", {}).get("rate", 1) <= .005
            and summary.get("checks", {}).get("rate", 0) >= .999
            and 0 <= summary.get("http_req_duration", {}).get("p(95)", float("inf")) < 300)


def payment_paths(checkout_pod, targets):
    """Проверить HTTP-доступ к метрикам по IP без выполнения платежа."""
    code = '''
import json,sys
from urllib.request import urlopen
result={}
for key,ip in json.loads(sys.argv[1]).items():
    try:
        with urlopen("http://"+ip+":8081/metrics",timeout=0.75) as response:
            result[key]={"reachable":response.status==200,"status":response.status}
    except Exception as error:
        result[key]={"reachable":False,"error_type":type(error).__name__}
print(json.dumps(result))
'''
    process = subprocess.run(["kubectl", "exec", "-n", "demo", checkout_pod, "-c", "checkout", "--", "python", "-c", code,
                              json.dumps(targets)], capture_output=True, text=True, timeout=15)
    if process.returncode:
        raise RuntimeError("Контроль сетевых путей не завершен")
    return json.loads(process.stdout)


def network_cut_proven(case):
    before, after = case.get("paths_before", {}), case.get("paths_during", {})
    return (before.get("blocked", {}).get("reachable") is True and before.get("alternate", {}).get("reachable") is True
            and after.get("blocked", {}).get("reachable") is False and after.get("alternate", {}).get("reachable") is True)


def payment_mesh(checkout_pod):
    """Считать только состояние и счетчики endpoint платежа, без конфигурации TLS."""
    code = '''
import json,time
from urllib.request import urlopen
data=json.load(urlopen("http://127.0.0.1:15000/clusters?format=json",timeout=5))
hosts={}
for cluster in data.get("cluster_statuses",[]):
    if cluster.get("name")=="outbound|80||payment.demo.svc.cluster.local":
        for host in cluster.get("host_statuses",[]):
            stats={row["name"]:int(row.get("value",0)) for row in host.get("stats",[])}
            hosts[host["address"]["socket_address"]["address"]]={
                "failed_outlier_check":host.get("health_status",{}).get("failed_outlier_check",False),
                "connect_failures":stats.get("cx_connect_fail",0),"requests_succeeded":stats.get("rq_success",0)}
print(json.dumps({"time":time.time(),"hosts":hosts}))
'''
    process = subprocess.run(["kubectl", "exec", "-n", "demo", checkout_pod, "-c", "checkout", "--", "python", "-c", code],
                              capture_output=True, text=True, timeout=15)
    if process.returncode:
        raise RuntimeError("Состояние резервного маршрута payment не получено")
    return json.loads(process.stdout)


def network_mesh_proven(case):
    targets = case.get("payment_targets", {})
    before, after = case.get("mesh_before", {}).get("hosts", {}), case.get("mesh_during", {}).get("hosts", {})
    blocked, alternate = targets.get("blocked"), targets.get("alternate")
    return (blocked in before and blocked in after and alternate in before and alternate in after
            and after[blocked]["failed_outlier_check"] is True
            and after[blocked]["connect_failures"] > before[blocked]["connect_failures"]
            and after[alternate]["requests_succeeded"] > before[alternate]["requests_succeeded"])


def scale_identity(obj):
    return {"uid": obj["metadata"]["uid"], "generation": obj["metadata"]["generation"], "replicas": obj["spec"]["replicas"]}


def resize_checkout(expected, replicas, marker, remove_marker=False):
    current = deployment("checkout", "demo")
    if scale_identity(current) != expected or (remove_marker and current["metadata"].get("annotations", {}).get("mtc-sre/slo-test") != marker):
        raise RuntimeError("Масштаб checkout изменен после начала опыта; запись запрещена")
    body = {"metadata": {"uid": expected["uid"], "resourceVersion": current["metadata"]["resourceVersion"],
                         "annotations": {"mtc-sre/slo-test": None if remove_marker else marker}}, "spec": {"replicas": replicas}}
    result = json.loads(cmd("kubectl", "patch", "deployment", "checkout", "-n", "demo", "--type=merge", "-p", json.dumps(body), "-o", "json"))
    return scale_identity(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default=os.getenv("CLUSTER", "mtc"))
    parser.add_argument("--output", default="docs/results/limited-chaos.json")
    parser.add_argument("--faults", nargs="+", choices=list(KINDS), default=list(KINDS))
    args = parser.parse_args()
    os.environ["CLUSTER"] = args.cluster
    cmd("sh", "scripts/require-context.sh", args.cluster)
    if json.loads(cmd("kubectl", "get", "networkchaos,podchaos,stresschaos", "-n", "demo", "-o", "json"))["items"]:
        raise SystemExit("В demo уже есть хаос; проверка не начата")
    jobs = json.loads(cmd("kubectl", "get", "jobs", "-n", "demo", "-o", "json"))["items"]
    if any(row["metadata"]["name"] == "k6-spike" or row["metadata"]["name"].startswith("k6-slo-") for row in jobs):
        raise SystemExit("В demo уже есть нагрузочный опыт; проверка не начата")
    if (deployment("checkout", "demo")["spec"]["replicas"] != 2 or deployment("payment", "demo")["spec"]["replicas"] != 2
        or deployment("healer", "heal")["spec"]["replicas"] != 1):
        raise SystemExit("Для проверки нужны по две реплики checkout/payment и один healer")
    wait_healthy(PROM, timeout=300)
    initial = wait_incident_closed()
    original_checkout = scale_identity(deployment("checkout", "demo"))
    original_background = deployment("k6-background", "demo")["spec"]["replicas"]
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "passed": False, "cases": [],
              "source_commit": cmd("git", "rev-parse", "HEAD").strip(), "initial_incident": initial,
              "source_worktree_dirty": bool(cmd("git", "status", "--porcelain")),
              "profile": {"client_rps": 2, "duration_per_case_seconds": 180, "warmup_seconds": 60,
                          "replicas": 3, "original_checkout_replicas": original_checkout["replicas"], "payment_replicas": 2, "network_action": "partition",
                          "network_scope": "Один checkout -> один payment; второй payment остается доступен",
                          "cpu_workers": 1, "cpu_load_percent_of_core": 5, "faults": args.faults},
              "resources_per_replica": deployment("checkout", "demo")["spec"]["template"]["spec"]["containers"][0]["resources"],
              "limitation": "Доказательство относится к указанной области отказа и короткому окну, не ко всем интенсивностям хаоса или месячному SLO. Полный сетевой разрыв и перегрузка сохраняют отрицательные результаты."}
    owned = []
    scale_owner = None
    scale_marker = uuid.uuid4().hex
    try:
        cmd("kubectl", "scale", "deployment/k6-background", "-n", "demo", "--replicas=0")
        scale_owner = resize_checkout(original_checkout, 3, scale_marker)
        cmd("kubectl", "rollout", "status", "deployment/checkout", "-n", "demo", "--timeout=180s")
        report["incident_before_client"] = wait_incident_closed()
        for fault in args.faults:
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
            warmup_end = time.time() + 60
            while time.time() < warmup_end:
                time.sleep(5)
            pods = json.loads(cmd("kubectl", "get", "pods", "-n", "demo", "-l", "app=checkout", "-o", "json"))["items"]
            pod = next(row for row in pods if not row["metadata"].get("deletionTimestamp") and row["status"]["phase"] == "Running")
            case.update(target_pod=pod["metadata"]["name"], target_uid=pod["metadata"]["uid"])
            payment_pod, payment_targets = None, None
            if fault == "network":
                payments = json.loads(cmd("kubectl", "get", "pods", "-n", "demo", "-l", "app=payment", "-o", "json"))["items"]
                payments = [row for row in payments if not row["metadata"].get("deletionTimestamp")
                            and any(condition["type"] == "Ready" and condition["status"] == "True" for condition in row["status"].get("conditions", []))]
                if len(payments) != 2:
                    raise RuntimeError("Два готовых пути payment не подтверждены")
                payment_pod = payments[0]["metadata"]["name"]
                payment_targets = {"blocked": payments[0]["status"]["podIP"], "alternate": payments[1]["status"]["podIP"]}
                case["payment_pods"] = {"blocked": payment_pod, "alternate": payments[1]["metadata"]["name"]}
                case["payment_targets"] = payment_targets
                case["paths_before"] = payment_paths(case["target_pod"], payment_targets)
                case["mesh_before"] = payment_mesh(case["target_pod"])
                if not all(row["reachable"] for row in case["paths_before"].values()):
                    raise RuntimeError("Исходные сетевые пути payment недоступны")
            case["injected_at"] = time.time()
            chaos = fault_manifest(fault, case["target_pod"], payment_pod)
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
                    if fault == "network" and "paths_during" not in case:
                        case["paths_during"] = payment_paths(case["target_pod"], payment_targets)
                    if fault == "network" and not network_mesh_proven(case):
                        case["mesh_during"] = payment_mesh(case["target_pod"])
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
                              and (fault != "network" or network_cut_proven(case))
                              and (fault != "network" or network_mesh_proven(case))
                              and case["samples"][-1]["rollout_ready"] and all(sample["replicas"] == 3 for sample in case["samples"]))
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
        cleanup_errors = []
        for kind, object_name in owned:
            try:
                cmd("kubectl", "delete", kind, object_name, "-n", "demo", "--ignore-not-found", "--timeout=60s")
            except Exception as error:
                cleanup_errors.append({"resource": kind + "/" + object_name, "error_type": type(error).__name__})
        if scale_owner is not None:
            try:
                resize_checkout(scale_owner, original_checkout["replicas"], scale_marker, remove_marker=True)
                cmd("kubectl", "rollout", "status", "deployment/checkout", "-n", "demo", "--timeout=180s")
                report["original_checkout_replicas_restored"] = scale_identity(deployment("checkout", "demo"))["replicas"] == original_checkout["replicas"]
            except Exception as error:
                cleanup_errors.append({"resource": "deployment/checkout", "error_type": type(error).__name__})
        try:
            cmd("kubectl", "scale", "deployment/k6-background", "-n", "demo", "--replicas=" + str(original_background))
        except Exception as error:
            cleanup_errors.append({"resource": "deployment/k6-background", "error_type": type(error).__name__})
        report["cleanup_errors"] = cleanup_errors
        report["passed"] = report["passed"] and not cleanup_errors and report.get("original_checkout_replicas_restored") is True
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    if not report["passed"]:
        raise SystemExit(report.get("error", "Удержание SLO не подтверждено"))


if __name__ == "__main__":
    main()
