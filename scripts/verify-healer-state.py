"""Проверить замену healer после реального действия и единственного владельца Lease."""

import argparse
import base64
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from experiment import cmd, deployment, wait_healthy


def document():
    obj = json.loads(cmd("kubectl", "get", "configmap", "healer-state", "-n", "heal", "-o", "json"))
    return json.loads(obj.get("data", {}).get("state.json", "{}"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default=os.getenv("CLUSTER", "mtc"))
    parser.add_argument("--output", default="docs/results/healer-state.json")
    args = parser.parse_args()
    os.environ["CLUSTER"] = args.cluster
    cmd("sh", "scripts/require-context.sh", args.cluster)
    if json.loads(cmd("kubectl", "get", "networkchaos,podchaos", "-n", "demo", "-o", "json"))["items"]:
        raise SystemExit("В demo уже есть Chaos Mesh; проверка не начата")
    original = deployment("healer", "heal")["spec"]["replicas"]
    if original != 1:
        raise SystemExit("Для проверки требуется один исходный экземпляр healer")
    token = base64.b64decode(json.loads(cmd("kubectl", "get", "secret", "healer-webhook", "-n", "heal", "-o", "json"))["data"]["token"]).decode()
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "passed": False}
    forward = None

    def post():
        payload = {"alerts": [{"status": "firing", "labels": {"service": "checkout", "alertname": "CheckoutSlow"},
                              "startsAt": datetime.fromtimestamp(started, timezone.utc).isoformat()}]}
        request = Request("http://127.0.0.1:18084/webhook", data=json.dumps(payload).encode(),
                          headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with urlopen(request, timeout=30) as response:
            return json.load(response)

    try:
        wait_healthy("http://127.0.0.1:19090", timeout=300)
        previous = {row["id"] for row in document().get("operations", [])}
        forward = subprocess.Popen(["kubectl", "port-forward", "-n", "heal", "svc/healer", "18084:8080"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cmd("sh", "chaos/inject.sh", "slow-pod")
        started, deadline = time.time(), time.time() + 240
        saved = None
        while time.time() < deadline:
            try:
                post()
            except Exception:
                pass
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
        forward.terminate()
        forward = None
        cmd("kubectl", "rollout", "restart", "deployment/healer", "-n", "heal")
        cmd("kubectl", "rollout", "status", "deployment/healer", "-n", "heal", "--timeout=180s")
        restored = document()
        if restored["state"]["_actions"] != state["_actions"] or restored["state"]["_cooldown_until"] != state["_cooldown_until"]:
            raise RuntimeError("Замена пода сбросила лимит или cooldown")
        if not any(row["id"] == operations[0]["id"] for row in restored["operations"]):
            raise RuntimeError("Замена пода потеряла журнал операции")
        report.update(action="delete_pod", action_budget_preserved=True, cooldown_preserved=True, journal_preserved=True)
        cmd("kubectl", "scale", "deployment/healer", "-n", "heal", "--replicas=2")
        cmd("kubectl", "rollout", "status", "deployment/healer", "-n", "heal", "--timeout=180s")
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
    finally:
        if forward:
            forward.terminate()
        cmd("sh", "chaos/inject.sh", "clear")
        cmd("kubectl", "scale", "deployment/healer", "-n", "heal", "--replicas=" + str(original))
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    if not report["passed"]:
        raise SystemExit(report.get("error", "Состояние healer не проверено"))
    print("Проверены сохранение состояния и исключительное владение Lease", flush=True)


if __name__ == "__main__":
    main()
