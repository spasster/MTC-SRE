"""Проверить метрики healer и безопасную реакцию на потерю сбора checkout."""

import argparse
import base64
import copy
import hashlib
import json
import os
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from experiment import cmd, scalar, wait_healthy, wait_incident_closed, resource, run_command, OWNER_ANNOTATION

PROM = "http://127.0.0.1:19090"


class OwnedMonitor:
    """Сбор временно приостанавливается в том же объекте, без пересоздания ServiceMonitor."""

    def __init__(self, original, marker):
        self.original = self.expected = original
        self.marker, self.changed, self.unknown = marker, False, False

    def update(self, spec, restore=False):
        for _ in range(3):
            current = resource("servicemonitor", "checkout", "demo")
            if (current is None or current["metadata"]["uid"] != self.expected["metadata"]["uid"]
                    or current["metadata"]["generation"] != self.expected["metadata"]["generation"]
                    or current["spec"] != self.expected["spec"]
                    or current["metadata"].get("annotations", {}).get(OWNER_ANNOTATION) != (self.marker if self.changed else None)):
                raise RuntimeError("ServiceMonitor изменен или заменен другим участником; запись запрещена")
            annotations = copy.deepcopy(current["metadata"].get("annotations", {}))
            if restore:
                annotations.pop(OWNER_ANNOTATION, None)
            else:
                annotations[OWNER_ANNOTATION] = self.marker
            body = [{"op": "test", "path": "/metadata/uid", "value": current["metadata"]["uid"]},
                    {"op": "test", "path": "/metadata/resourceVersion", "value": current["metadata"]["resourceVersion"]},
                    {"op": "replace", "path": "/spec", "value": spec},
                    {"op": "add", "path": "/metadata/annotations", "value": annotations}]
            self.unknown = True
            response = run_command("kubectl", "patch", "servicemonitor", "checkout", "-n", "demo", "--type=json",
                                   "-p", json.dumps(body), "-o", "json", timeout=30)
            if response.returncode:
                if "Error from server (Conflict)" in response.stderr:
                    self.unknown = False
                    continue
                raise RuntimeError("API не подтвердил изменение ServiceMonitor; повтор запрещен")
            result = json.loads(response.stdout)
            generation = result["metadata"].get("generation")
            if (result["metadata"].get("uid") != current["metadata"]["uid"] or result.get("spec") != spec
                    or type(generation) is not int or generation < current["metadata"]["generation"]
                    or spec != current["spec"] and generation == current["metadata"]["generation"]
                    or result["metadata"].get("annotations", {}).get(OWNER_ANNOTATION) != (None if restore else self.marker)):
                raise RuntimeError("Ответ API не подтвердил ожидаемое изменение ServiceMonitor")
            self.expected, self.changed, self.unknown = result, not restore, False
            return
        raise RuntimeError("Три конфликта CAS; ServiceMonitor сохранен без дополнительной записи")

    def pause(self):
        spec = copy.deepcopy(self.original["spec"])
        spec["selector"] = {"matchLabels": {"mtc-sre/monitor-paused": self.marker}}
        self.update(spec)

    def cleanup(self):
        if self.unknown:
            raise RuntimeError("Результат изменения ServiceMonitor неизвестен; автоматический возврат запрещен")
        if self.changed:
            self.update(self.original["spec"], restore=True)


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
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = sample()
        if time.monotonic() < deadline and predicate(result):
            return result
        time.sleep(5)
    raise RuntimeError("Условие наблюдаемости не подтвердилось в срок")


def close_test_incident(payload, token, report):
    """Поздний resolved допустим после закрытия worker; итог проверяется в сохраненном состоянии."""
    report["test_incident_closed"] = False
    wait_healthy(PROM, timeout=180)
    resolved = copy.deepcopy(payload)
    resolved["alerts"][0]["status"] = "resolved"
    request = Request("http://127.0.0.1:18085/webhook", data=json.dumps(resolved).encode(),
                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urlopen(request, timeout=30) as response:
        result = json.load(response)
    report["resolved_webhook_result"] = [{key: row.get(key) for key in ("result", "action", "mode")}
                                         for row in result.get("results", [])]
    acknowledgements = report["resolved_webhook_result"]
    if (len(acknowledgements) != 1
            or acknowledgements[0]["result"] not in ("resolved", "ignored: no active incident")):
        raise RuntimeError("Webhook не подтвердил обработку завершения тестового инцидента")
    report["closed_state"] = wait_incident_closed(timeout=180)
    report["test_incident_closed"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default=os.getenv("CLUSTER", "mtc"))
    parser.add_argument("--output", default="docs/results/healer-observability.json")
    args = parser.parse_args()
    cmd("sh", "scripts/require-context.sh", args.cluster)
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "passed": False,
              "verifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    forward = None
    monitor_owner = None
    fired = False
    try:
        wait_healthy(PROM, timeout=300)
        report["initial_incident"] = wait_incident_closed()
        monitor = json.loads(cmd("kubectl", "get", "servicemonitor", "checkout", "-n", "demo", "-o", "json"))
        monitor_owner = OwnedMonitor(monitor, uuid.uuid4().hex)
        report["monitor_uid"] = monitor["metadata"]["uid"]
        report["healthy"] = wait(lambda row: all(row[key] == 1 for key in ("leader", "state_available", "evidence_complete", "scrape_up")), 120)
        before = operations()
        token = base64.b64decode(json.loads(cmd("kubectl", "get", "secret", "healer-webhook", "-n", "heal", "-o", "json"))["data"]["token"]).decode()
        forward = subprocess.Popen(["kubectl", "port-forward", "-n", "heal", "svc/healer", "18085:8080"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        monitor_owner.pause()
        report["missing"] = wait(lambda row: row["evidence_complete"] == 0, 120)
        payload = {"alerts": [{"status": "firing", "labels": {"service": "checkout", "alertname": "CheckoutSlow"},
                              "startsAt": datetime.now(timezone.utc).isoformat()}]}
        request = Request("http://127.0.0.1:18085/webhook", data=json.dumps(payload).encode(),
                          headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
        fired = True
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
        if monitor_owner is not None:
            restore_phase = "возврат ServiceMonitor"
            try:
                was_changed = monitor_owner.changed
                monitor_owner.cleanup()
                if was_changed:
                    restore_phase = "подтверждение восстановления сбора"
                    report["restored"] = wait(lambda row: row["evidence_complete"] == 1 and row["evidence_alert_firing"] == 0, 180)
                if fired:
                    restore_phase = "подтверждение закрытия инцидента"
                    close_test_incident(payload, token, report)
            except Exception as exc:
                report["passed"] = False
                report["restore_failed_phase"] = restore_phase
                report["restore_error_type"] = type(exc).__name__
                report["restore_error"] = f"Не завершен этап: {restore_phase}"
        if forward:
            try:
                forward.terminate()
                forward.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    forward.kill()
                    forward.wait(timeout=10)
                except Exception:
                    report["passed"] = False
                    report["forward_error"] = "Завершение port-forward не подтверждено"
            except Exception:
                report["passed"] = False
                report["forward_error"] = "Завершение port-forward не подтверждено"
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    if not report["passed"]:
        raise SystemExit(report.get("error", "Наблюдаемость не подтверждена"))
    print("Метрики и алерт потери данных проверены; новых операций нет, сбор восстановлен.")


if __name__ == "__main__":
    main()
