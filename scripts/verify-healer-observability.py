"""Проверить метрики healer и безопасную реакцию на потерю сбора checkout."""

import argparse
import base64
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from experiment import cmd, scalar, wait_healthy

PROM = "http://127.0.0.1:19090"


def sample():
    fields = ("leader", "state_available", "evidence_complete", "operation_unknown", "incident_actions",
              "state_checked_timestamp_seconds", "evidence_checked_timestamp_seconds", "evidence_age_seconds")
    result = {name: scalar(PROM, f'max(healer_{name}{{namespace="heal"}})') for name in fields}
    result["scrape_up"] = scalar(PROM, 'min(up{namespace="heal",job="healer-metrics"})')
    result["evidence_alert_firing"] = scalar(PROM, 'count(ALERTS{service="healer",alertname="HealerEvidenceMissing",alertstate="firing"}) or vector(0)')
    return result


def operations():
    obj = json.loads(cmd("kubectl", "get", "configmap", "healer-state", "-n", "heal", "-o", "json"))
    return {row["id"] for row in json.loads(obj["data"]["state.json"])["operations"]}


def wait(predicate, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = sample()
        if predicate(result):
            return result
        time.sleep(5)
    raise RuntimeError("Условие наблюдаемости не подтвердилось в срок")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default=os.getenv("CLUSTER", "mtc"))
    parser.add_argument("--output", default="docs/results/healer-observability.json")
    args = parser.parse_args()
    cmd("sh", "scripts/require-context.sh", args.cluster)
    wait_healthy(PROM, timeout=300)
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "passed": False}
    monitor = json.loads(cmd("kubectl", "get", "servicemonitor", "checkout", "-n", "demo", "-o", "json"))
    monitor["metadata"] = {key: value for key, value in monitor["metadata"].items()
                           if key in ("name", "namespace", "labels", "annotations")}
    monitor.pop("status", None)
    forward = None
    removed = False
    try:
        report["healthy"] = wait(lambda row: all(row[key] == 1 for key in ("leader", "state_available", "evidence_complete", "scrape_up")), 120)
        before = operations()
        token = base64.b64decode(json.loads(cmd("kubectl", "get", "secret", "healer-webhook", "-n", "heal", "-o", "json"))["data"]["token"]).decode()
        forward = subprocess.Popen(["kubectl", "port-forward", "-n", "heal", "svc/healer", "18085:8080"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cmd("kubectl", "delete", "servicemonitor", "checkout", "-n", "demo")
        removed = True
        report["missing"] = wait(lambda row: row["evidence_complete"] == 0, 120)
        payload = {"alerts": [{"status": "firing", "labels": {"service": "checkout", "alertname": "CheckoutSlow"},
                              "startsAt": datetime.now(timezone.utc).isoformat()}]}
        request = Request("http://127.0.0.1:18085/webhook", data=json.dumps(payload).encode(),
                          headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
        report["webhook_result"] = [{key: row.get(key) for key in ("result", "action", "mode")} for row in result.get("results", [])]
        if len(report["webhook_result"]) != 1 or report["webhook_result"][0]["action"] != "none":
            raise RuntimeError("Webhook не подтвердил отказ от действия при отсутствии данных")
        report["alert"] = wait(lambda row: row["evidence_alert_firing"] == 1 and row["leader"] == 1 and row["state_available"] == 1, 180)
        report["new_operations"] = len(operations() - before)
        if report["new_operations"]:
            raise RuntimeError("Потеря данных привела к новой операции")
        report["passed"] = True
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        if removed:
            try:
                subprocess.run(["kubectl", "apply", "-f", "-"], input=json.dumps(monitor), text=True, capture_output=True, check=True)
                report["restored"] = wait(lambda row: row["evidence_complete"] == 1 and row["evidence_alert_firing"] == 0, 180)
            except Exception:
                report["passed"] = False
                report["restore_error"] = "Восстановление сбора не подтверждено; проверить ServiceMonitor demo/checkout"
        if forward:
            forward.terminate()
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    if not report["passed"]:
        raise SystemExit(report.get("error", "Наблюдаемость не подтверждена"))
    print("Метрики и алерт потери данных проверены; новых операций нет, сбор восстановлен.")


if __name__ == "__main__":
    main()
