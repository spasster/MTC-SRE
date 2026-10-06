"""Проверить локальный отчёт и ограниченные права SRE-ассистента."""

import argparse
import base64
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from experiment import cmd


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default="mtc")
    parser.add_argument("--output", default="docs/results/llm.json")
    parser.add_argument("--kind", choices=["report", "postmortem"], default="report")
    args = parser.parse_args()
    cmd("sh", "scripts/require-context.sh", args.cluster)
    permissions = {}
    for verb, resource, expected in [("get", "deployments", "yes"), ("patch", "deployments", "no"),
                                     ("delete", "pods", "no"), ("get", "secrets", "no")]:
        result = subprocess.run(["kubectl", "auth", "can-i", verb, resource, "-n", "demo",
                                 "--as=system:serviceaccount:heal:sre-assistant"], capture_output=True, text=True)
        permissions[verb + " " + resource] = result.stdout.strip()
        if result.stdout.strip() != expected:
            raise SystemExit("Права ассистента не соответствуют режиму только на чтение")
    for verb, resource, expected in [("get", "configmap/healer-state", "yes"), ("patch", "configmap/healer-state", "no"),
                                     ("get", "secrets", "no"), ("patch", "lease/healer-leader", "no")]:
        result = subprocess.run(["kubectl", "auth", "can-i", verb, resource, "-n", "heal",
                                 "--as=system:serviceaccount:heal:sre-assistant"], capture_output=True, text=True)
        permissions[verb + " heal/" + resource] = result.stdout.strip()
        if result.stdout.strip() != expected:
            raise SystemExit("Доступ к журналу допускает только чтение указанного ConfigMap")
    secret = json.loads(cmd("kubectl", "get", "secret", "healer-webhook", "-n", "heal", "-o", "json"))
    token = base64.b64decode(secret["data"]["token"]).decode()
    forward = subprocess.Popen(["kubectl", "port-forward", "-n", "heal", "svc/sre-assistant", "18084:8082"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(2)
        started = time.time()
        request = Request("http://127.0.0.1:18084/" + args.kind, data=b"", headers={"Authorization": "Bearer " + token})
        with urlopen(request, timeout=150) as response:
            result = json.load(response)
        if result.get("actions_applied") != [] or not result.get("report"):
            raise SystemExit("Отчёт ассистента не соответствует контракту")
        if args.kind == "postmortem" and not result.get("incident_facts", {}).get("started_at"):
            raise SystemExit("В журнале нет подтвержденного времени начала инцидента")
        report = {"checked_at": datetime.now(timezone.utc).isoformat(), "passed": True,
                  "duration_seconds": time.time() - started, "rbac": permissions, "response": result}
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        print("Получен настоящий локальный отчёт, права записи и чтения Secret запрещены.")
    finally:
        forward.terminate()


if __name__ == "__main__":
    main()
