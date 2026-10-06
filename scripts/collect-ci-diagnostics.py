"""Сохранить состояние тестового контура без журналов, Secret и переменных среды."""

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def read(resource, namespace=None):
    command = ["kubectl", "get", resource, "-o", "json", "--request-timeout=10s"]
    if namespace:
        command += ["-n", namespace]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    return json.loads(result.stdout).get("items", []) if result.returncode == 0 else []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=".runtime/proof/diagnostics.json")
    args = parser.parse_args()
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "pods": [], "events": []}
    for namespace in ("heal", "demo", "obs"):
        for pod in read("pods", namespace):
            status = pod.get("status", {})
            containers = []
            for container in status.get("containerStatuses", []) + status.get("initContainerStatuses", []):
                row = {key: container.get(key) for key in ("name", "ready", "restartCount", "imageID")}
                for key in ("state", "lastState"):
                    row[key] = {kind: {field: value for field, value in values.items() if field != "message"}
                                for kind, values in container.get(key, {}).items()}
                containers.append(row)
            report["pods"].append({"namespace": namespace, "name": pod["metadata"]["name"],
                                   "phase": status.get("phase"), "conditions": status.get("conditions", []),
                                   "containers": containers})
        for event in read("events", namespace):
            report["events"].append({"namespace": namespace, "reason": event.get("reason"),
                                     "type": event.get("type"), "count": event.get("count"),
                                     "lastTimestamp": event.get("lastTimestamp"),
                                     "object_kind": event.get("involvedObject", {}).get("kind"),
                                     "object_name": event.get("involvedObject", {}).get("name")})
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print("Состояние подов и причины событий сохранены без конфигурации и журналов приложений.")


if __name__ == "__main__":
    main()
