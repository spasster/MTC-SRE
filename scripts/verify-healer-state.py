"""Проверить замену healer после реального действия и единственного владельца Lease."""

import argparse
import base64
import hashlib
import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from experiment import cmd, deployment, wait_healthy, wait_incident_closed, OwnedDeployment, OwnedResource


def stop_forward(forward):
    forward.terminate()
    try:
        forward.wait(timeout=10)
    except subprocess.TimeoutExpired:
        forward.kill()
        forward.wait(timeout=10)


def document():
    obj = json.loads(cmd("kubectl", "get", "configmap", "healer-state", "-n", "heal", "-o", "json"))
    return json.loads(obj.get("data", {}).get("state.json", "{}"))


def webhook_outcome(body):
    """Сохраняются классификации ответов, без detail, операторов и данных подтверждения."""
    outcomes = []
    for row in body.get("results", []):
        allowed = {"action": {"none", "delete_pod", "scale", "rollback", "restart_redis"},
                   "mode": {"ignore", "auto", "approval", "escalate"},
                   "diagnosis": {"unknown", "settling", "slow_pod", "cpu", "redis", "payment", "partition", "bad_release"}}
        outcome = {key: row.get(key) if row.get(key) in values else None for key, values in allowed.items()}
        result = row.get("result", "")
        prefixes = ("ignored:", "needs approval:", "waiting approval", "escalated", "failed:", "skipped:")
        outcome["result_class"] = next((prefix.rstrip(":") for prefix in prefixes if result.startswith(prefix)), "other")
        known_ignored = {"ignored: cooling down", "ignored: watching", "ignored: healthy", "ignored: recovering", "ignored: waiting approval"}
        if result in known_ignored:
            outcome["ignored_reason"] = result
        outcomes.append(outcome)
    return outcomes


def diagnostic_evidence():
    """Один снимок перед очисткой объясняет отказ, без записи через Kubernetes API."""
    code = "import json; from app.evidence import gather; print(json.dumps(gather('demo')))"
    evidence = json.loads(cmd("kubectl", "exec", "-n", "heal", "deployment/healer", "-c", "healer", "--", "python", "-c", code, timeout=30))
    keys = ("replicas", "request_rate", "p95", "revision_age_seconds", "error_ratio", "redis_error_ratio", "payment_timeout_ratio",
            "payment_error_ratio", "application_error_ratio", "synthetic_success", "synthetic_age_seconds", "telemetry_complete",
            "telemetry_age_seconds", "collection_seconds", "redis_ready", "payment_ready")
    safe = {key: evidence.get(key) for key in keys if evidence.get(key) is None or type(evidence[key]) in (int, float, bool)}
    safe["pods"] = [{key: pod.get(key) for key in ("pod", "uid", "p95", "p95_recent", "throttle")} for pod in evidence.get("pods", [])]
    safe["error_present"] = "error" in evidence
    known_errors = {"Срок сбора телеметрии истек", "Нет рабочих подов текущей ревизии checkout", "Неполные метрики текущих подов checkout",
                    "Метрики checkout недоступны или устарели", "К окончанию сбора метрики или синтетическая проверка устарели"}
    if safe["error_present"]:
        safe["error_reason"] = evidence["error"] if evidence["error"] in known_errors else "Внешняя или нераспознанная ошибка чтения"
    return safe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default=os.getenv("CLUSTER", "mtc"))
    parser.add_argument("--output", default=".runtime/healer-state.json")
    args = parser.parse_args()
    os.environ["CLUSTER"] = args.cluster
    cmd("sh", "scripts/require-context.sh", args.cluster)
    if json.loads(cmd("kubectl", "get", "networkchaos,podchaos", "-n", "demo", "-o", "json"))["items"]:
        raise SystemExit("В demo уже есть Chaos Mesh; проверка не начата")
    original = deployment("healer", "heal")
    if original["spec"]["replicas"] != 1:
        raise SystemExit("Для проверки требуется один исходный экземпляр healer")
    token = base64.b64decode(json.loads(cmd("kubectl", "get", "secret", "healer-webhook", "-n", "heal", "-o", "json"))["data"]["token"]).decode()
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "passed": False,
              "verifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    marker = uuid.uuid4().hex
    healer_owner = OwnedDeployment("healer", "heal", original, marker)
    owned_resources = []
    forward = None
    injection_started = False

    def post():
        payload = {"alerts": [{"status": "firing", "labels": {"service": "checkout", "alertname": "CheckoutSlow"},
                              "startsAt": datetime.fromtimestamp(started, timezone.utc).isoformat()}]}
        request = Request("http://127.0.0.1:18084/webhook", data=json.dumps(payload).encode(),
                          headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with urlopen(request, timeout=30) as response:
            return json.load(response)

    try:
        wait_healthy("http://127.0.0.1:19090", timeout=300)
        report["initial_incident"] = wait_incident_closed()
        previous = {row["id"] for row in document().get("operations", [])}
        forward = subprocess.Popen(["kubectl", "port-forward", "-n", "heal", "svc/healer", "18084:8080"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pods = json.loads(cmd("kubectl", "get", "pods", "-n", "demo", "-l", "app=checkout", "-o", "json"))["items"]
        ready = [pod for pod in pods if not pod["metadata"].get("deletionTimestamp")
                 and any(row.get("name") == "checkout" and row.get("ready") is True for row in pod.get("status", {}).get("containerStatuses", []))]
        if not ready:
            raise RuntimeError("Рабочий под checkout для инъекции не найден")
        selected = ready[0]
        manifest = json.loads(cmd("kubectl", "create", "-f", "chaos/checkout-slow-pod.yaml", "--dry-run=client", "-o", "json"))
        manifest["spec"]["selector"]["pods"]["demo"] = [selected["metadata"]["name"]]
        injection = OwnedResource(manifest, marker)
        owned_resources.append(injection)
        injection_started = True
        injection.create(timeout=30)
        report["target_pod_uid"] = selected["metadata"]["uid"]
        started, deadline = time.time(), time.time() + 240
        saved = None
        report["webhook_attempts"] = []
        while time.time() < deadline:
            try:
                outcome = {"elapsed_seconds": time.time() - started, "outcomes": webhook_outcome(post())}
            except Exception as error:
                outcome = {"elapsed_seconds": time.time() - started, "exception_type": type(error).__name__}
                if type(getattr(error, "code", None)) is int:
                    outcome["http_status"] = error.code
            report["webhook_attempts"].append(outcome)
            saved = document()
            operations = [row for row in saved.get("operations", []) if row["id"] not in previous and row["status"] == "applied"]
            if operations:
                break
            time.sleep(5)
        if not operations or operations[0]["action"] != "delete_pod":
            raise RuntimeError("Реальное удаление медленного пода не подтверждено")
        state = saved["state"]
        if state["_actions"] < 1 or state["_cooldown_until"] <= time.time():
            raise RuntimeError("Лимит и cooldown не сохранены после действия")
        stop_forward(forward)
        forward = None
        healer_owner.mutate(1, restart=True)
        cmd("kubectl", "rollout", "status", "deployment/healer", "-n", "heal", "--timeout=180s", timeout=210)
        restored = document()
        if restored["state"]["_actions"] != state["_actions"] or restored["state"]["_cooldown_until"] != state["_cooldown_until"]:
            raise RuntimeError("Замена пода сбросила лимит или cooldown")
        if not any(row["id"] == operations[0]["id"] for row in restored["operations"]):
            raise RuntimeError("Замена пода потеряла журнал операции")
        report.update(action="delete_pod", action_budget_preserved=True, cooldown_preserved=True, journal_preserved=True)
        healer_owner.mutate(2)
        cmd("kubectl", "rollout", "status", "deployment/healer", "-n", "heal", "--timeout=180s", timeout=210)
        time.sleep(35)
        statuses = []
        pods = json.loads(cmd("kubectl", "get", "pods", "-n", "heal", "-l", "app=healer", "-o", "json"))["items"]
        for pod in pods:
            if pod["metadata"].get("deletionTimestamp"):
                continue
            code = "from app.state import KubernetesStore; s=KubernetesStore(); print(s.acquire())"
            # Отдельный процесс не имеет права отнять действующую Lease у сервера.
            statuses.append(cmd("kubectl", "exec", "-n", "heal", pod["metadata"]["name"], "-c", "healer", "--", "python", "-c", code).strip())
        if len(statuses) != 2 or statuses != ["False", "False"]:
            raise RuntimeError("Дополнительный процесс получил действующую Lease")
        lease = json.loads(cmd("kubectl", "get", "lease", "healer-leader", "-n", "heal", "-o", "json"))
        owner = lease["spec"]["holderIdentity"].split(":")[0]
        if sum(pod["metadata"]["uid"] == owner for pod in pods) != 1:
            raise RuntimeError("Владелец Lease не совпадает с одним живым подом")
        report.update(two_instances=True, single_holder=True, extra_processes_refused=True, passed=True)
    except Exception as exc:
        report["error"] = str(exc)
        if injection_started:
            try:
                report["evidence_before_cleanup"] = diagnostic_evidence()
            except Exception as error:
                report["diagnostic_error_type"] = type(error).__name__
    finally:
        cleanup_errors = []
        def cleanup(operation, action):
            try:
                action()
            except Exception as error:
                cleanup_errors.append({"operation": operation, "error_type": type(error).__name__})
        if forward:
            cleanup("port-forward", lambda: stop_forward(forward))
        for owned in reversed(owned_resources):
            cleanup(owned.kind + "/" + owned.name, owned.cleanup)
        cleanup("deployment/healer", healer_owner.cleanup)
        if report["passed"] and not cleanup_errors:
            def verify_closed_state():
                cmd("kubectl", "rollout", "status", "deployment/healer", "-n", "heal", "--timeout=180s", timeout=210)
                wait_healthy("http://127.0.0.1:19090", timeout=180)
                report["closed_state"] = wait_incident_closed(timeout=180)
            cleanup("закрытое состояние после возврата", verify_closed_state)
        report["cleanup_errors"] = cleanup_errors
        report["resource_intents"] = [{"kind": obj.kind, "namespace": obj.namespace, "name": obj.name,
                                       "uid_confirmed": obj.uid, "generation_confirmed": obj.generation} for obj in owned_resources]
        if cleanup_errors:
            report["passed"] = False
            report.setdefault("error", "Очистка проверки состояния не подтверждена")
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    if not report["passed"]:
        raise SystemExit(report.get("error", "Состояние healer не проверено"))
    print("Проверены сохранение состояния и исключительное владение Lease", flush=True)


if __name__ == "__main__":
    main()
