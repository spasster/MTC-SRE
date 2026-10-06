"""Проверить реальную кнопку выделенного Telegram-бота на тестовом кластере."""

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from experiment import cmd, deployment, wait_healthy
from importlib import import_module
document = import_module("verify-healer-state").document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default=os.getenv("CLUSTER", "mtc"))
    parser.add_argument("--case", choices=["approve-release", "reject-redis"], required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    os.environ["CLUSTER"] = args.cluster
    cmd("sh", "scripts/require-context.sh", args.cluster)
    if json.loads(cmd("kubectl", "get", "networkchaos,podchaos", "-n", "demo", "-o", "json"))["items"]:
        raise SystemExit("Сначала завершить текущую инъекцию Chaos Mesh")
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "case": args.case, "passed": False}
    injected = False
    try:
        wait_healthy("http://127.0.0.1:19090", timeout=300)
        target = "checkout" if args.case == "approve-release" else "redis"
        before = deployment(target, "demo")
        started = time.time()
        cmd("sh", "chaos/inject.sh", "bad-release" if target == "checkout" else "redis")
        injected = True
        deadline = time.time() + args.timeout
        announced = False
        selected = None
        while time.time() < deadline:
            saved = document()
            pending = saved.get("state", {}).get("_pending")
            events = [row for row in saved.get("events", []) if row.get("time", 0) >= started]
            if pending and not announced:
                announced = True
                selected = pending
                print("Ожидается кнопка в НОВОМ тестовом боте: " + ("Разрешить" if target == "checkout" else "Отклонить"), flush=True)
            answered = [row for row in events if row["event"] == "approval_callback"]
            if answered:
                choice = "approve" if target == "checkout" else "reject"
                if answered[-1]["choice"] != choice:
                    raise RuntimeError("Получено другое решение оператора")
                after = deployment(target, "demo")
                report.update(real_callback_received=True, authorized_operator_recorded=True,
                              choice=choice, pending_consumed=saved["state"].get("_pending") is None)
                if choice == "approve":
                    operations = [row for row in saved["operations"] if row.get("prepared_at", 0) >= started]
                    if not any(row["action"] == "rollback" and row["status"] == "applied" for row in operations):
                        raise RuntimeError("Принятие rollback API не подтверждено журналом")
                    if after["metadata"]["generation"] <= selected["deployment_identity"]["generation"]:
                        raise RuntimeError("Deployment после подтверждения не изменился")
                    wait_healthy("http://127.0.0.1:19090", timeout=300)
                    report.update(api_action_applied=True, slo_restored=True)
                else:
                    if after["metadata"]["generation"] != before["metadata"]["generation"]:
                        raise RuntimeError("Отказ оператора изменил Redis")
                    report.update(api_action_applied=False, deployment_unchanged=True)
                report["passed"] = True
                break
            time.sleep(3)
        if not report["passed"]:
            raise RuntimeError("Реальная кнопка Telegram не получена вовремя")
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        if injected and target == "checkout" and not report.get("api_action_applied"):
            cmd("sh", "chaos/inject.sh", "undo-release")
        if injected:
            cmd("sh", "chaos/inject.sh", "clear")
        file = Path(args.output)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    if not report["passed"]:
        raise SystemExit(report["error"])
    print("Проверена реальная кнопка Telegram и результат в Kubernetes", flush=True)


if __name__ == "__main__":
    main()
