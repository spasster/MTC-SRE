"""Проверить реакцию реального healer на убийство пода и отказ зависимостей."""

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
import yaml

from experiment import api, cmd, deployment, metrics, wait_healthy, wait_incident_closed, OwnedDeployment, OwnedResource


def inject(scenario, marker, owned):
    """Намерение сохраняется до POST/patch; общая очистка namespace не используется."""
    if scenario == "bad-release":
        original = deployment("checkout", "demo")
        owner = OwnedDeployment("checkout", "demo", original, marker)
        owned.append(("deployment/checkout", owner, True))
        owner.mutate(original["spec"]["replicas"], environment={"FAIL_RATE": "1"})
        return owner.expected["metadata"]["uid"]
    files = {"kill": "chaos/checkout-kill.yaml", "partition": "chaos/partition-payment.yaml", "redis": "chaos/redis-slow.yaml"}
    text = Path(files[scenario]).read_text(encoding="utf-8")
    if scenario == "redis":
        pods = json.loads(cmd("kubectl", "get", "pods", "-n", "demo", "-l", "app=redis", "-o", "json"))["items"]
        running = [row for row in pods if row.get("status", {}).get("phase") == "Running" and not row["metadata"].get("deletionTimestamp")]
        if len(running) != 1:
            raise RuntimeError("Единственный текущий под Redis не подтвержден")
        text = text.replace("POD_NAME", running[0]["metadata"]["name"])
    owner = OwnedResource(yaml.safe_load(text), marker)
    owned.append((owner.kind + "/" + owner.name, owner, False))
    owner.create(timeout=30)
    return owner.uid


def cleanup_owned(owned, errors):
    for name, owner, restore_template in reversed(list(owned)):
        try:
            if restore_template:
                owner.cleanup(restore_template=True)
            else:
                owner.cleanup()
            owned.remove((name, owner, restore_template))
        except Exception as error:
            errors.append({"resource": name, "error_type": type(error).__name__})


def stop_forward(forward, errors):
    if forward is None:
        return True
    try:
        try:
            forward.terminate()
        except Exception as error:
            errors.append({"resource": "process/port-forward", "error_type": type(error).__name__})
            forward.kill()
        try:
            forward.wait(timeout=5)
        except subprocess.TimeoutExpired:
            forward.kill()
            forward.wait(timeout=5)
        return True
    except Exception as error:
        errors.append({"resource": "process/port-forward", "error_type": type(error).__name__})
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default="mtc")
    parser.add_argument("--output", default=".runtime/scenarios.json")
    args = parser.parse_args()
    os.environ["CLUSTER"] = args.cluster
    cmd("sh", "scripts/require-context.sh", args.cluster)
    if json.loads(cmd("kubectl", "get", "networkchaos,podchaos", "-n", "demo", "-o", "json"))["items"]:
        raise SystemExit("В demo уже есть Chaos Mesh. Проверка не начата.")
    if any(row["metadata"]["name"] == "k6-spike" for row in json.loads(
        cmd("kubectl", "get", "jobs", "-n", "demo", "-o", "json"))["items"]):
        raise SystemExit("В demo уже есть k6-spike. Проверка не начата.")
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "cases": [], "passed": False, "cleanup_errors": [],
              "source_commit": cmd("git", "rev-parse", "HEAD").strip(),
              "verifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    forward, owned, marker = None, [], uuid.uuid4().hex

    def pods(app):
        return sorted(row["metadata"]["uid"] for row in json.loads(
            cmd("kubectl", "get", "pods", "-n", "demo", "-l", "app=" + app, "-o", "json"))["items"]
            if not row["metadata"].get("deletionTimestamp"))

    try:
        secret = json.loads(cmd("kubectl", "get", "secret", "healer-webhook", "-n", "heal", "-o", "json"))
        token = base64.b64decode(secret["data"]["token"]).decode()
        forward = subprocess.Popen(["kubectl", "port-forward", "-n", "heal", "svc/healer", "18083:8080"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for scenario, expected, app in [("kill", None, "checkout"), ("partition", ("partition", "payment"), "checkout"),
                                         ("bad-release", "bad_release", "checkout"), ("redis", "redis", "redis")]:
            wait_healthy("http://127.0.0.1:19090", timeout=300)
            wait_incident_closed()
            before = pods(app)
            case = {"scenario": scenario, "passed": False}
            report["cases"].append(case)
            case["injection_uid"] = inject(scenario, marker, owned)
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
                case.update(replacement_observed=True, healthy_after=True, healer_action_applied=False)
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
                case.update(diagnosis=result["diagnosis"], action=result["action"], result=result["result"], error_ratio=sample["error_ratio"],
                            replicas=deployment("checkout", "demo")["spec"]["replicas"], pods_unchanged_after_detection=pods(app) == before)
                if not case["pods_unchanged_after_detection"]:
                    raise RuntimeError("Во время отказа зависимости неожиданно заменён под")
            cleanup_owned(owned, report["cleanup_errors"])
            if report["cleanup_errors"]:
                raise RuntimeError("Очистка собственного сценария не подтверждена")
            wait_healthy("http://127.0.0.1:19090", timeout=300)
            case["passed"] = True
            print("Проверено: " + scenario, flush=True)
        report["passed"] = True
    except Exception as exc:
        report["error"] = str(exc)
    except KeyboardInterrupt:
        report["error"] = "Проверка остановлена оператором"
    finally:
        try:
            cleanup_owned(owned, report["cleanup_errors"])
        finally:
            try:
                report["forward_stopped"] = stop_forward(forward, report["cleanup_errors"])
            finally:
                report["passed"] = report["passed"] and not report["cleanup_errors"] and not owned and report.get("forward_stopped") is True
                report["unfinished_resources"] = [name for name, _, _ in owned]
                report["finished_at"] = datetime.now(timezone.utc).isoformat()
                target = Path(args.output)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    if not report["passed"]:
        raise SystemExit(report.get("error", "Сценарии не проверены"))


if __name__ == "__main__":
    main()
