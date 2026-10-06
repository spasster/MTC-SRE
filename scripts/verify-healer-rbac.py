"""Проверить разрешенные и запрещенные действия healer без изменения объектов."""

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import subprocess

from experiment import cmd


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default=os.getenv("CLUSTER", "mtc"))
    parser.add_argument("--output", default="docs/results/healer-rbac.json")
    args = parser.parse_args()
    cmd("sh", "scripts/require-context.sh", args.cluster)
    checks = [
        ("get", "deployments/checkout", "demo", True),
        ("get", "deployments/redis", "demo", True),
        ("get", "deployments/payment", "demo", True),
        ("patch", "deployments/checkout", "demo", True),
        ("patch", "deployments/redis", "demo", True),
        ("patch", "deployments/payment", "demo", False),
        ("patch", "deployments/other", "demo", False),
        ("update", "deployments/checkout", "demo", False),
        ("update", "deployments/payment", "demo", False),
        ("list", "deployments", "demo", False),
        ("list", "replicasets", "demo", True),
        ("list", "pods", "demo", True),
        ("delete", "pods", "demo", True),
        ("get", "jobs/k6-spike", "demo", True),
        ("get", "jobs/other", "demo", False),
        ("get", "secrets", "heal", False),
        ("patch", "configmaps/healer-state", "heal", True),
        ("patch", "configmaps/other", "heal", False),
    ]
    rows = []
    for verb, resource, namespace, expected in checks:
        result = subprocess.run(["kubectl", "auth", "can-i", verb, resource, "-n", namespace,
                                 "--as=system:serviceaccount:heal:healer"], capture_output=True, text=True, timeout=15)
        answer = result.stdout.strip()
        rows.append({"verb": verb, "resource": resource, "namespace": namespace, "expected": expected,
                     "allowed": answer == "yes", "checked": answer in {"yes", "no"}})
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "checks": rows,
              "passed": all(row["checked"] and row["allowed"] == row["expected"] for row in rows),
              "limitation": "RBAC не ограничивает динамические имена подов по метке. Удаление подов разрешено только в demo; выбор checkout дополнительно проверяется кодом по UID."}
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    if not report["passed"]:
        raise SystemExit("Разрешения healer не соответствуют ожидаемым")
    print("Проверены разрешенные и запрещенные действия healer", flush=True)


if __name__ == "__main__":
    main()
