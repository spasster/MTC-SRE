"""Сохранить состояние тестового контура без журналов, Secret и переменных среды."""

import argparse
import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def read(resource, namespace=None):
    command = ["kubectl", "get", resource, "-o", "json", "--request-timeout=10s"]
    if namespace:
        command += ["-n", namespace]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    return json.loads(result.stdout).get("items", []) if result.returncode == 0 else []


def healer_state():
    """Только поля допуска и счетчики журнала, без имен операторов и содержимого сообщений."""
    try:
        result = subprocess.run(["kubectl", "get", "configmap", "healer-state", "-n", "heal", "-o", "json",
                                 "--request-timeout=10s"], capture_output=True, text=True, timeout=15)
        if result.returncode:
            return {"available": False, "error_type": "KubernetesReadError"}
        obj = json.loads(result.stdout)
        raw = obj.get("data", {}).get("state.json")
        if raw is None:
            return {"available": True, "data_present": False}
        document = json.loads(raw)
        state = document.get("state")
        if not isinstance(state, dict):
            return {"available": True, "data_present": True, "snapshot_is_object": False}
        flags = ("_escalated", "_scale_down_pending", "_scaled_by_healer", "_recovery_pending", "_resolved_pending", "_mttr_sent", "_uncertain")
        numbers = ("_actions", "_broke_at", "_fixed_at", "_applied_at", "_cooldown_until")
        def number(value):
            try:
                return value if type(value) in (int, float) and math.isfinite(value) else None
            except OverflowError:
                return None
        safe = {key: state.get(key) if type(state.get(key)) is bool else None for key in flags}
        safe.update({key: number(state.get(key)) for key in numbers})
        safe.update({key + "_present": state.get(key) is not None for key in ("_pending", "_action_intent", "_scale_owner", "_last_recovery")})
        safe.update({key + "_count": len(state[key]) if isinstance(state.get(key), list) else None for key in ("_closed", "_recovery_targets")})
        return {"available": True, "data_present": True, "snapshot_is_object": True, "state": safe,
                "initialized": obj.get("metadata", {}).get("annotations", {}).get("mtc-sre/state-initialized") == "true",
                "operations_count": len(document["operations"]) if isinstance(document.get("operations"), list) else None,
                "events_count": len(document["events"]) if isinstance(document.get("events"), list) else None}
    except Exception as error:
        return {"available": False, "error_type": type(error).__name__}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=".runtime/proof/diagnostics.json")
    args = parser.parse_args()
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "pods": [], "events": [], "nodes": [], "healer_state": healer_state()}
    for node in read("nodes"):
        report["nodes"].append({"capacity": node.get("status", {}).get("capacity"),
                                "allocatable": node.get("status", {}).get("allocatable"),
                                "conditions": node.get("status", {}).get("conditions")})
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
                                   "resources": [{"name": row["name"], "resources": row.get("resources", {})}
                                                 for row in pod.get("spec", {}).get("containers", []) + pod.get("spec", {}).get("initContainers", [])],
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
