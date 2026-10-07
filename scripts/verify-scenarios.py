"""Проверить реакцию реального healer на убийство пода и отказ зависимостей."""

import argparse
import base64
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from experiment import api, cmd, deployment, metrics, wait_healthy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default="mtc")
    parser.add_argument("--output", default="docs/results/scenarios.json")
    args = parser.parse_args()
    os.environ["CLUSTER"] = args.cluster
    cmd("sh", "scripts/require-context.sh", args.cluster)
    if json.loads(cmd("kubectl", "get", "networkchaos,podchaos", "-n", "demo", "-o", "json"))["items"]:
        raise SystemExit("В demo уже есть Chaos Mesh. Проверка не начата.")
    if any(row["metadata"]["name"] == "k6-spike" for row in json.loads(
        cmd("kubectl", "get", "jobs", "-n", "demo", "-o", "json"))["items"]):
        raise SystemExit("В demo уже есть k6-spike. Проверка не начата.")
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "cases": [], "passed": False}
    secret = json.loads(cmd("kubectl", "get", "secret", "healer-webhook", "-n", "heal", "-o", "json"))
    token = base64.b64decode(secret["data"]["token"]).decode()
    forward = subprocess.Popen(["kubectl", "port-forward", "-n", "heal", "svc/healer", "18083:8080"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    running = None

    def pods(app):
        return sorted(row["metadata"]["uid"] for row in json.loads(
            cmd("kubectl", "get", "pods", "-n", "demo", "-l", "app=" + app, "-o", "json"))["items"]
            if not row["metadata"].get("deletionTimestamp"))

    try:
        for scenario, expected, app in [("kill", None, "checkout"), ("partition", ("partition", "payment"), "checkout"),
                                         ("bad-release", "bad_release", "checkout"), ("redis", "redis", "redis")]:
            wait_healthy("http://127.0.0.1:19090", timeout=300)
            before = pods(app)
            cmd("sh", "chaos/inject.sh", scenario)
            running = scenario
            started = time.time()
            if scenario == "bad-release":
                cmd("kubectl", "rollout", "status", "deployment/checkout", "-n", "demo", "--timeout=180s", timeout=210)
                before = pods(app)
            if scenario == "kill":
                wait_healthy("http://127.0.0.1:19090", timeout=180)
                if pods(app) == before:
                    raise RuntimeError("PodChaos не заменил под checkout")
                status = api("http://127.0.0.1:18083/status", token=token)
                latest = max(status.get("api_accepted_at") or 0, (status.get("last_recovery") or {}).get("api_accepted_at") or 0)
                if latest >= started:
                    raise RuntimeError("Короткое убийство пода вызвало действие healer")
                case = {"scenario": scenario, "replacement_observed": True, "healthy_after": True, "healer_action_applied": False}
            else:
                deadline, result = time.time() + 180, None
                while time.time() < deadline:
                    sample = metrics("http://127.0.0.1:19090")
                    if sample["error_ratio"] is not None and sample["error_ratio"] > .2:
                        payload = {"alerts": [{"status": "firing", "labels": {"service": "checkout", "alertname": "CheckoutHighErrorRate"},
                                               "startsAt": datetime.fromtimestamp(started, timezone.utc).isoformat()}]}
                        request = Request("http://127.0.0.1:18083/webhook", data=json.dumps(payload).encode(),
                                          headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
                        with urlopen(request, timeout=20) as response:
                            result = json.load(response)["results"][0]
                        if result.get("diagnosis") in (expected if isinstance(expected, tuple) else (expected,)):
                            break
                    time.sleep(5)
                if not result or result.get("diagnosis") not in (expected if isinstance(expected, tuple) else (expected,)):
                    raise RuntimeError("Ожидаемый диагноз не подтверждён: " + scenario)
                if scenario == "partition" and (result.get("action") != "none" or result.get("mode") != "escalate"):
                    raise RuntimeError("Сетевой разрыв вызвал изменение кластера")
                if (scenario in ("bad-release", "redis")
                    and not (result.get("result", "").startswith("needs approval:")
                             or result.get("result") in ("waiting approval", "ignored: waiting approval"))):
                    raise RuntimeError("Опасное действие не остановлено без подтверждения")
                case = {"scenario": scenario, "diagnosis": result["diagnosis"], "action": result["action"],
                        "result": result["result"], "error_ratio": sample["error_ratio"],
                        "replicas": deployment("checkout", "demo")["spec"]["replicas"],
                        "pods_unchanged_after_detection": pods(app) == before}
                if not case["pods_unchanged_after_detection"]:
                    raise RuntimeError("Во время отказа зависимости неожиданно заменён под")
            if scenario == "bad-release":
                cmd("sh", "chaos/inject.sh", "undo-release")
            else:
                cmd("sh", "chaos/inject.sh", "clear")
            running = None
            wait_healthy("http://127.0.0.1:19090", timeout=300)
            case["passed"] = True
            report["cases"].append(case)
            print("Проверено: " + scenario, flush=True)
        report["passed"] = True
    except Exception as exc:
        report["error"] = str(exc)
    except KeyboardInterrupt:
        report["error"] = "Проверка остановлена оператором"
    finally:
        if running == "bad-release":
            cmd("sh", "chaos/inject.sh", "undo-release")
        if running:
            cmd("sh", "chaos/inject.sh", "clear")
        forward.terminate()
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    if not report["passed"]:
        raise SystemExit(report.get("error", "Сценарии не проверены"))


if __name__ == "__main__":
    main()
