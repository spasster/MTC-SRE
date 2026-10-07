"""Сравнение восстановления в одинаковом локальном контуре k3d."""

import argparse
import base64
import copy
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

PROMQL = {
    "p95": 'histogram_quantile(0.95,sum by(le)(rate(http_request_duration_seconds_bucket{namespace="demo",job="checkout",path="/work"}[1m])))',
    "request_rate": 'sum(rate(http_requests_total{namespace="demo",job="checkout",path="/work"}[1m]))',
    "error_ratio": '(sum(rate(http_requests_total{namespace="demo",job="checkout",path="/work",status=~"5.."}[1m])) or vector(0)) / sum(rate(http_requests_total{namespace="demo",job="checkout",path="/work"}[1m]))',
    "synthetic_success": 'min(probe_success{service="checkout",target_namespace="demo"})',
    "synthetic_time": 'min(timestamp(probe_success{service="checkout",target_namespace="demo"}))',
    "alerts_firing": 'count(ALERTS{service="checkout",alertstate="firing"}) or vector(0)',
    "alerts_active": 'count(ALERTS{service="checkout"}) or vector(0)',
    "cpu_throttle": 'max(rate(container_cpu_cfs_throttled_periods_total{namespace="demo",container="checkout"}[1m]) / rate(container_cpu_cfs_periods_total{namespace="demo",container="checkout"}[1m]))',
}


def run_command(*args, timeout=60, input_text=None):
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Команде нужен конечный положительный таймаут")
    if args and args[0] == "kubectl" and not any(str(arg).startswith("--request-timeout") for arg in args):
        args = (args[0], "--request-timeout=" + format(min(timeout, 10), ".6f") + "s", *args[1:])
    try:
        return subprocess.run(args, input=input_text, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Команда " + str(args[0]) + " не завершилась за отведенное время") from None


def cmd(*args, timeout=60, input_text=None):
    result = run_command(*args, timeout=timeout, input_text=input_text)
    if result.returncode:
        raise RuntimeError("Команда не выполнена: " + " ".join(args) + "\n" + result.stderr[-1500:])
    return result.stdout


def api(url, params=None, token=None):
    request = Request(url + ("?" + urlencode(params) if params else ""))
    if token:
        request.add_header("Authorization", "Bearer " + token)
    with urlopen(request, timeout=10) as response:
        return json.load(response)


def deployment(name, namespace):
    return json.loads(cmd("kubectl", "get", "deployment", name, "-n", namespace, "-o", "json"))


OWNER_ANNOTATION = "mtc-sre/experiment"
RESOURCE_PATHS = {"job": "/apis/batch/v1", "pod": "/api/v1", **dict.fromkeys(
    ("networkchaos", "podchaos", "stresschaos"), "/apis/chaos-mesh.org/v1alpha1")}
RESOURCE_PLURALS = {"job": "jobs", "pod": "pods", "networkchaos": "networkchaos", "podchaos": "podchaos", "stresschaos": "stresschaos"}


class RejectedConflict(RuntimeError):
    """API явно отклонил запись по CAS; неизвестный результат сюда не относится."""


def resource(kind, name, namespace):
    raw = cmd("kubectl", "get", kind, name, "-n", namespace, "--ignore-not-found", "-o", "json")
    return json.loads(raw) if raw.strip() else None


def delete_identity(kind, name, namespace, current):
    """DELETE проверяет UID и resourceVersion атомарно, включая замену по тому же имени."""
    metadata = current["metadata"]
    path = RESOURCE_PATHS[kind] + "/namespaces/" + quote(namespace, safe="") + "/" + RESOURCE_PLURALS[kind] + "/" + quote(name, safe="")
    options = {"apiVersion": "v1", "kind": "DeleteOptions", "propagationPolicy": "Foreground",
               "preconditions": {"uid": metadata["uid"], "resourceVersion": metadata["resourceVersion"]}}
    response = run_command("kubectl", "delete", "--raw=" + path, "-f", "-", input_text=json.dumps(options), timeout=30)
    if response.returncode:
        if "Error from server (Conflict)" in response.stderr:
            raise RejectedConflict("Удаление отклонено из-за конфликта CAS")
        raise RuntimeError("API не подтвердил удаление объекта; повтор неизвестного результата запрещен")


class OwnedResource:
    """Намерение создания записывается до API; неизвестный POST не повторяется."""

    def __init__(self, manifest, marker):
        self.manifest, self.marker = manifest, marker
        self.kind = manifest["kind"].lower()
        self.name, self.namespace = manifest["metadata"]["name"], manifest["metadata"]["namespace"]
        self.uid, self.generation = None, None
        self.expected_spec = None
        manifest["metadata"].setdefault("annotations", {})[OWNER_ANNOTATION] = marker

    def create(self, timeout):
        result = json.loads(cmd("kubectl", "create", "-f", "-", "-o", "json",
                                input_text=json.dumps(self.manifest), timeout=timeout))
        self.uid, self.generation = result["metadata"]["uid"], result["metadata"]["generation"]
        self.expected_spec = copy.deepcopy(result["spec"])
        if result["metadata"].get("annotations", {}).get(OWNER_ANNOTATION) != self.marker:
            raise RuntimeError("API не подтвердил владение созданным объектом")

    def current(self):
        current = resource(self.kind, self.name, self.namespace)
        if current is None:
            return None
        metadata = current["metadata"]
        generation = self.generation if self.generation is not None else 1
        # У Chaos Mesh без status subresource контроллер увеличивает generation при записи статуса.
        # Только полный spec, подтвержденный ответом create, разрешает такой служебный рост.
        unchanged_spec = self.expected_spec is not None and current.get("spec") == self.expected_spec
        status_generation = (self.kind in ("networkchaos", "podchaos", "stresschaos") and unchanged_spec and self.uid is not None
                             and type(metadata.get("generation")) is int and metadata["generation"] >= generation)
        if (metadata.get("annotations", {}).get(OWNER_ANNOTATION) != self.marker
                or self.uid is not None and metadata["uid"] != self.uid
                or self.expected_spec is not None and not unchanged_spec
                or metadata["generation"] != generation and not status_generation):
            raise RuntimeError("Объект опыта заменен или изменен; очистка запрещена")
        return current

    def cleanup(self):
        for _ in range(3):
            current = self.current()
            if current is None:
                return
            uid = current["metadata"]["uid"]
            try:
                delete_identity(self.kind, self.name, self.namespace, current)
                break
            except RejectedConflict:
                continue
        else:
            raise RuntimeError("Три конфликта удаления; объект опыта сохранен")
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            remaining = resource(self.kind, self.name, self.namespace)
            if remaining is None or remaining["metadata"]["uid"] != uid:
                return
            time.sleep(2)
        raise RuntimeError("Удаление собственного объекта опыта не подтверждено")


class OwnedDeployment:
    """Возвращается только подтвержденное собственное изменение, с повторной проверкой CAS."""

    def __init__(self, name, namespace, original, marker):
        self.name, self.namespace, self.marker = name, namespace, marker
        self.original, self.expected = original, original
        self.changed, self.unknown = False, False

    def mutate(self, replicas, restart=False, restore=False, environment=None, restore_template=False):
        for _ in range(3):
            current = deployment(self.name, self.namespace)
            expected = self.expected
            annotation = current["metadata"].get("annotations", {}).get(OWNER_ANNOTATION)
            if (current["metadata"]["uid"] != expected["metadata"]["uid"]
                    or current["metadata"]["generation"] != expected["metadata"]["generation"]
                    or current["spec"] != expected["spec"]
                    or annotation != (self.marker if self.changed else None)):
                raise RuntimeError("Deployment изменен другим участником; запись запрещена")
            body = {"metadata": {"uid": current["metadata"]["uid"], "resourceVersion": current["metadata"]["resourceVersion"],
                                  "annotations": {OWNER_ANNOTATION: None if restore else self.marker}}, "spec": {"replicas": replicas}}
            requested_spec = copy.deepcopy(current["spec"])
            requested_spec["replicas"] = replicas
            if restart:
                body["spec"]["template"] = {"metadata": {"annotations": {"mtc-sre/experiment-restart": self.marker}}}
                requested_spec["template"].setdefault("metadata", {}).setdefault("annotations", {})["mtc-sre/experiment-restart"] = self.marker
            if environment is not None:
                containers = [row for row in requested_spec["template"]["spec"]["containers"] if row["name"] == self.name]
                if len(containers) != 1 or not all(isinstance(key, str) and isinstance(value, str) for key, value in environment.items()):
                    raise RuntimeError("Единственный контейнер и переменные тестового релиза не подтверждены")
                selected = containers[0]
                selected["env"] = [row for row in selected.get("env", []) if row["name"] not in environment]
                selected["env"] += [{"name": key, "value": value} for key, value in environment.items()]
                body["spec"]["template"] = copy.deepcopy(requested_spec["template"])
            if restore_template:
                requested_spec["template"] = copy.deepcopy(self.original["spec"]["template"])
                body["spec"]["template"] = copy.deepcopy(requested_spec["template"])
            self.unknown = True
            response = run_command("kubectl", "patch", "deployment", self.name, "-n", self.namespace,
                                   "--type=merge", "-p", json.dumps(body), "-o", "json", timeout=30)
            if response.returncode:
                if "Error from server (Conflict)" in response.stderr:
                    self.unknown = False
                    continue
                raise RuntimeError("API не подтвердил изменение Deployment; повтор запрещен")
            result = json.loads(response.stdout)
            generation = result["metadata"].get("generation")
            if (result["metadata"]["uid"] != current["metadata"]["uid"] or result["spec"] != requested_spec
                    or type(generation) is not int or generation < current["metadata"]["generation"]
                    or requested_spec != current["spec"] and generation == current["metadata"]["generation"]
                    or result["metadata"].get("annotations", {}).get(OWNER_ANNOTATION) != (None if restore else self.marker)):
                raise RuntimeError("Ответ API не подтвердил ожидаемое изменение Deployment")
            self.expected, self.changed, self.unknown = result, not restore, False
            return
        raise RuntimeError("Три конфликта CAS; Deployment сохранен без дополнительной записи")

    def cleanup(self, restore_template=False):
        if self.unknown:
            raise RuntimeError("Результат изменения Deployment неизвестен; автоматический возврат запрещен")
        if self.changed:
            self.mutate(self.original["spec"]["replicas"], restore=True, restore_template=restore_template)


def scalar(base, query):
    rows = api(base + "/api/v1/query", {"query": query}).get("data", {}).get("result", [])
    if not rows:
        return None
    value = float(rows[0]["value"][1])
    return value if math.isfinite(value) else None


def metrics(base):
    sample = {key: scalar(base, query) for key, query in PROMQL.items()}
    obj = deployment("checkout", "demo")
    desired = obj["spec"]["replicas"]
    status = obj.get("status", {})
    sample.update(
        time=time.time(), replicas=desired,
        rollout_ready=(status.get("observedGeneration", 0) >= obj["metadata"]["generation"]
                       and status.get("updatedReplicas", 0) == desired
                       and status.get("availableReplicas", 0) >= desired
                       and status.get("replicas", 0) == desired),
    )
    return sample


def healthy(sample):
    return (
        sample["p95"] is not None and sample["p95"] < 0.3
        and sample["error_ratio"] is not None and sample["error_ratio"] <= 0.005
        and sample["request_rate"] is not None and sample["request_rate"] > 0
        and sample["synthetic_success"] == 1 and sample["synthetic_time"] is not None
        and 0 <= sample["time"] - sample["synthetic_time"] <= 45 and sample["rollout_ready"]
    )


def wait_healthy(base, timeout=180):
    deadline, streak = time.time() + timeout, 0
    while time.time() < deadline:
        sample = metrics(base)
        if healthy(sample) and sample["alerts_active"] == 0:
            streak += 1
            if streak >= 3:
                return
        else:
            streak = 0
        time.sleep(5)
    raise RuntimeError("Начальный SLO не восстановлен; эксперимент не начат")


def closed_incident(state):
    """Готовность метрик не заменяет закрытый журнал предыдущего инцидента."""
    flags = ("_recovery_pending", "_resolved_pending", "_scaled_by_healer", "_scale_down_pending", "_uncertain")
    return (isinstance(state, dict) and type(state.get("_actions")) is int and state["_actions"] == 0
            and all(key in state and state[key] is None for key in ("_broke_at", "_fixed_at", "_applied_at", "_action_intent", "_pending"))
            and all(state.get(key) is False for key in flags)
            and state.get("_closed") == [] and state.get("_recovery_targets") == [])


def wait_incident_closed(timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        obj = json.loads(cmd("kubectl", "get", "configmap", "healer-state", "-n", "heal", "-o", "json", timeout=min(10, remaining)))
        if time.monotonic() >= deadline:
            break
        document = json.loads(obj.get("data", {}).get("state.json", "null"))
        state = document.get("state") if isinstance(document, dict) else None
        if closed_incident(state):
            return {"checked_at": time.time(), "started_at": None, "actions": 0, "operation_unknown": False,
                    "recovery_pending": False, "approval_pending": False, "incident_closed": True}
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    raise RuntimeError("Предыдущий инцидент healer не закрыт; новый эксперимент не начат")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default=os.getenv("CLUSTER", "mtc"))
    parser.add_argument("--scenario", choices=["slow-pod", "spike"], default="slow-pod")
    parser.add_argument("--mode", choices=["baseline", "auto"], required=True)
    parser.add_argument("--manual-delay", type=int, default=30)
    parser.add_argument("--timeout", type=int, default=420)
    parser.add_argument("--prometheus", default="http://127.0.0.1:19090")
    parser.add_argument("--output", required=True)
    parser.add_argument("--natural-end", action="store_true", help="Дождаться окончания spike и возврата масштаба самим healer")
    parser.add_argument("--incident-chain-output", help="Проверить exemplar, трейс и лог медленного запроса в этом инциденте")
    parser.add_argument("--spike-duration", type=int, help="Длительность дополнительной нагрузки в секундах для короткой демонстрации")
    parser.add_argument("--spike-rate", type=int, help="Дополнительные запросы в секунду; профиль фиксируется до инъекции")
    parser.add_argument("--spike-capacity", help="Завершенные замеры того же контура для выбора CPU-профиля")
    args = parser.parse_args()
    if args.natural_end and (args.scenario != "spike" or args.mode != "auto"):
        parser.error("--natural-end допустим только для auto spike")
    if args.spike_duration is not None and (args.scenario != "spike" or args.spike_duration < 120):
        parser.error("--spike-duration допустим для spike и должен быть не менее 120 с")
    if args.spike_rate is not None and (args.scenario != "spike" or args.spike_rate <= 0):
        parser.error("--spike-rate допустим только для spike и должен быть положительным")
    if args.spike_capacity and (args.scenario != "spike" or args.spike_rate is not None):
        parser.error("--spike-capacity допустим только для spike без отдельного --spike-rate")
    if args.timeout <= 0 or args.manual_delay < 0:
        parser.error("Время опыта должно быть положительным, задержка оператора - неотрицательной")
    os.environ["CLUSTER"] = args.cluster
    cmd("sh", "scripts/require-context.sh", args.cluster)
    existing = json.loads(cmd("kubectl", "get", "networkchaos,podchaos", "-n", "demo", "-o", "json"))
    if existing["items"]:
        raise SystemExit("В demo уже есть эксперимент Chaos Mesh. Сначала завершить его.")
    jobs = json.loads(cmd("kubectl", "get", "jobs", "-n", "demo", "-o", "json"))["items"]
    if any(item["metadata"]["name"] == "k6-spike" for item in jobs):
        raise SystemExit("В demo уже есть k6-spike. Сначала завершить его.")
    original_healer = deployment("healer", "heal")
    original_checkout = deployment("checkout", "demo")
    if original_checkout["spec"]["replicas"] != 2:
        raise SystemExit("Для сравнения требуется исходное состояние с двумя репликами checkout")
    marker = uuid.uuid4().hex
    healer_owner = OwnedDeployment("healer", "heal", original_healer, marker)
    checkout_owner = OwnedDeployment("checkout", "demo", original_checkout, marker)
    owned_resources = []
    background_owner = None
    background_restored = False
    token = None
    forward = None
    report = {"scenario": args.scenario, "mode": args.mode, "manual_delay_seconds": args.manual_delay,
              "created_at": datetime.now(timezone.utc).isoformat(), "samples": [], "passed": False,
              "source_commit": cmd("git", "rev-parse", "HEAD").strip(),
              "verifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "baseline_note": "Задержка оператора задаётся явно; это контролируемый baseline, не измерение работы человека."}
    try:
        wait_healthy(args.prometheus)
        report["healer_precondition_before_preparation"] = wait_incident_closed()
        if args.spike_capacity:
            from cpu_profile import selected_profile
            measured = json.loads(Path(args.spike_capacity).read_text(encoding="utf-8"))
            profile = selected_profile(measured, args.cluster, report["source_commit"])
            args.spike_rate = profile["total_rps"]
            report["cpu_profile"] = profile
            report["capacity_sha256"] = hashlib.sha256(Path(args.spike_capacity).read_bytes()).hexdigest()
            background_owner = OwnedDeployment("k6-background", "demo", deployment("k6-background", "demo"), marker)
        image_check = str(Path(".runtime") / ("experiment-images-" + marker + ".json"))
        cmd(sys.executable, "scripts/local-images.py", "--cluster", args.cluster, "--component", "checkout", "payment",
            "--verify-only", "--output", image_check, timeout=300)
        report["runtime_image_check"] = image_check
        if args.mode == "auto":
            # После baseline может потребоваться повторный импорт именно закрепленного тега.
            containers = original_healer["spec"]["template"]["spec"]["containers"]
            healer_image = next(row["image"] for row in containers if row["name"] == "healer")
            report["healer_image_id"] = cmd("docker", "image", "inspect", healer_image, "--format", "{{.Id}}").strip()
            cmd("k3d", "image", "import", healer_image, "-c", args.cluster, "--mode", "direct", timeout=180)
        healer_owner.mutate(0 if args.mode == "baseline" else 1, restart=args.mode == "auto")
        if args.mode == "auto":
            cmd("kubectl", "rollout", "status", "deployment/healer", "-n", "heal", "--timeout=180s", timeout=210)
            cmd(sys.executable, "scripts/local-images.py", "--cluster", args.cluster, "--component", "checkout", "payment", "healer",
                "--verify-only", "--output", image_check, timeout=300)
            secret = json.loads(cmd("kubectl", "get", "secret", "healer-webhook", "-n", "heal", "-o", "json"))
            token = base64.b64decode(secret["data"]["token"]).decode()
            forward = subprocess.Popen(["kubectl", "port-forward", "-n", "heal", "svc/healer", "18081:8080"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_healthy(args.prometheus)
        report["healer_precondition_before_injection"] = wait_incident_closed()
        selected_pod = json.loads(cmd("kubectl", "get", "pods", "-n", "demo", "-l", "app=checkout", "-o", "json"))["items"][0]
        selected = selected_pod["metadata"]["name"]
        if background_owner is not None:
            background_owner.mutate(0)
        report["injected_at"] = time.time()
        if args.scenario == "spike":
            manifest = json.loads(cmd("kubectl", "create", "-f", "k8s/k6-spike.yaml", "--dry-run=client", "-o", "json"))
            environment = manifest["spec"]["template"]["spec"]["containers"][0]["env"]
            if args.spike_duration:
                environment.append({"name": "DURATION", "value": str(args.spike_duration) + "s"})
                report["spike_duration_seconds"] = args.spike_duration
            if args.spike_rate:
                environment.append({"name": "RATE", "value": str(args.spike_rate)})
        else:
            manifest = json.loads(cmd("kubectl", "create", "-f", "chaos/checkout-slow-pod.yaml", "--dry-run=client", "-o", "json"))
            manifest["spec"]["selector"]["pods"]["demo"] = [selected]
        injection = OwnedResource(manifest, marker)
        owned_resources.append(injection)
        injection.create(timeout=min(60, args.timeout))
        report["injection_api_accepted_at"] = time.time()
        if args.scenario == "spike":
            report["additional_rps"] = args.spike_rate or 3
        report["target_pod"] = selected if args.scenario == "slow-pod" else None
        deadline, streak = time.time() + args.timeout, 0
        while time.time() < deadline:
            sample = metrics(args.prometheus)
            report["samples"].append(sample)
            if background_owner is not None and not background_restored:
                job = owned_resources[0].current()
                if job is None:
                    raise RuntimeError("Собственный CPU Job исчез до завершения опыта")
                if job.get("status", {}).get("succeeded") or job.get("status", {}).get("failed"):
                    background_owner.cleanup()
                    background_restored = True
                    report["background_restored_after_job"] = True
                    if not report.get("slo_restored_at"):
                        raise RuntimeError("SLO не подтвержден до окончания CPU-нагрузки; эффект scale не доказан")
            if not healthy(sample) and report.get("detected_at") is None:
                report["detected_at"] = sample["time"]
            if sample["alerts_firing"] and report.get("alert_firing_at") is None:
                report["alert_firing_at"] = sample["time"]
            if args.incident_chain_output and not report.get("incident_chain_verified") and sample["p95"] and sample["p95"] > .3:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise RuntimeError("Время опыта истекло до проверки наблюдаемости")
                result = run_command(sys.executable, "scripts/verify-observability.py", "--since", str(report["injected_at"]),
                                     "--min-duration", ".3", "--output", args.incident_chain_output, timeout=min(90, remaining))
                report["incident_chain_verified"] = result.returncode == 0
            if args.mode == "baseline" and report.get("alert_firing_at") and not report.get("api_accepted_at"):
                if sample["time"] >= report["alert_firing_at"] + args.manual_delay:
                    if args.scenario == "slow-pod":
                        for _ in range(3):
                            current = resource("pod", selected, "demo")
                            if current is None or current["metadata"]["uid"] != selected_pod["metadata"]["uid"]:
                                raise RuntimeError("Выбранный под заменен; ручное удаление запрещено")
                            try:
                                delete_identity("pod", selected, "demo", current)
                                break
                            except RejectedConflict:
                                continue
                        else:
                            raise RuntimeError("Удаление пода трижды отклонено по CAS")
                    else:
                        checkout_owner.mutate(5)
                    report["api_accepted_at"] = time.time()
            elif args.mode == "auto":
                status = api("http://127.0.0.1:18081/status", token=token)
                if status.get("started_at") is not None:
                    if status["started_at"] < report["injected_at"]:
                        raise RuntimeError("Журнал healer содержит начало предыдущего инцидента")
                    report["healer_started_at"] = status["started_at"]
                timing = status if (status.get("api_accepted_at") or 0) >= report["injected_at"] else (status.get("last_recovery") or {})
                accepted = timing.get("api_accepted_at")
                if accepted and accepted >= report["injected_at"]:
                    report["api_accepted_at"] = accepted
                restored = timing.get("slo_restored_at")
                if restored and restored >= report["injected_at"]:
                    report["healer_slo_restored_at"] = restored
            print(f"После внесения сбоя {sample['time'] - report['injected_at']:.0f} с: "
                  f"p95={sample['p95']}, доля ошибок={sample['error_ratio']}, реплик={sample['replicas']}", flush=True)
            if report.get("api_accepted_at") and healthy(sample):
                if args.natural_end and report.get("slo_restored_at"):
                    job = json.loads(cmd("kubectl", "get", "job", "k6-spike", "-n", "demo", "-o", "json"))
                    if job.get("status", {}).get("failed"):
                        raise RuntimeError("Spike завершился ошибкой")
                    if job.get("status", {}).get("succeeded") and sample["replicas"] == 2:
                        report.update(natural_job_completion=True, autonomous_scale_down=True, scale_down_at=sample["time"], passed=True)
                        break
                    time.sleep(5)
                    continue
                if args.scenario == "spike":
                    job = json.loads(cmd("kubectl", "get", "job", "k6-spike", "-n", "demo", "-o", "json"))
                    if job.get("status", {}).get("succeeded"):
                        report["error"] = "SLO восстановился после завершения нагрузки; результат scale не подтверждён"
                        break
                streak += 1
                if streak >= 3:
                    report["slo_restored_at"] = sample["time"]
                    report["mttr_seconds"] = sample["time"] - report["injected_at"]
                    report["detection_seconds"] = report["detected_at"] - report["injected_at"]
                    report["api_acceptance_seconds"] = report["api_accepted_at"] - report["injected_at"]
                    report["passed"] = not args.natural_end
                    if not args.natural_end:
                        break
            else:
                streak = 0
            time.sleep(5)
        if not report["passed"]:
            report.setdefault("error", "Восстановление и требуемый возврат масштаба не подтверждены до таймаута")
        if args.incident_chain_output and not report.get("incident_chain_verified"):
            report.update(passed=False, error="Связка метрика -> трейс -> лог на инциденте не подтверждена")
    except Exception as exc:
        report["error"] = str(exc)
    except KeyboardInterrupt:
        report["error"] = "Эксперимент остановлен оператором; восстановление не подтверждено"
    finally:
        cleanup_errors = []
        def cleanup(operation, action):
            try:
                action()
            except Exception as error:
                cleanup_errors.append({"operation": operation, "error_type": type(error).__name__})
        if forward is not None:
            try:
                forward.terminate()
                forward.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    forward.kill()
                    forward.wait(timeout=10)
                except Exception as error:
                    cleanup_errors.append({"operation": "port-forward", "error_type": type(error).__name__})
            except Exception as error:
                cleanup_errors.append({"operation": "port-forward", "error_type": type(error).__name__})
        if owned_resources and args.scenario == "spike":
            try:
                owned_job = owned_resources[-1].current()
                pods = json.loads(cmd("kubectl", "get", "pods", "-n", "demo", "-l", "job-name=k6-spike", "-o", "json"))["items"]
                for pod in pods:
                    if owned_job is not None and any(owner.get("kind") == "Job" and owner.get("uid") == owned_job["metadata"]["uid"]
                        and owner.get("controller") is True for owner in pod["metadata"].get("ownerReferences", [])):
                        logs = cmd("kubectl", "logs", "-n", "demo", pod["metadata"]["name"], "-c", "k6")
                        for line in logs.splitlines():
                            if line.startswith("MTC_SUMMARY "):
                                report["k6_summary"] = json.loads(line[len("MTC_SUMMARY "):])
                if "k6_summary" not in report:
                    report["k6_summary_error"] = "Полный итог клиента k6 не получен до удаления собственного Job"
            except Exception:
                report["k6_summary_error"] = "Итог клиента k6 не получен"
        for owned in reversed(owned_resources):
            cleanup(owned.kind + "/" + owned.name, owned.cleanup)
        cleanup("deployment/checkout", checkout_owner.cleanup)
        if background_owner is not None:
            cleanup("deployment/k6-background", background_owner.cleanup)
        if args.mode == "auto" and owned_resources:
            # Масштаб checkout принадлежит healer; опыт не присваивает эту запись себе.
            cleanup("закрытие инцидента healer", lambda: (wait_healthy(args.prometheus), wait_incident_closed()))
        if healer_owner.changed and not healer_owner.unknown and original_healer["spec"]["replicas"] > 0:
            def import_original_healer():
                image = next(row["image"] for row in original_healer["spec"]["template"]["spec"]["containers"] if row["name"] == "healer")
                cmd("k3d", "image", "import", image, "-c", args.cluster, "--mode", "direct", timeout=180)
            cleanup("импорт исходного healer", import_original_healer)
        if args.mode == "auto" and original_healer["spec"]["replicas"] == 0 and cleanup_errors:
            cleanup_errors.append({"operation": "deployment/healer", "error_type": "Инцидент не закрыт; healer оставлен работающим"})
        else:
            cleanup("deployment/healer", healer_owner.cleanup)
        report["cleanup_errors"] = cleanup_errors
        report["resource_intents"] = [{"kind": obj.kind, "namespace": obj.namespace, "name": obj.name,
                                        "uid_confirmed": obj.uid, "generation_confirmed": obj.generation} for obj in owned_resources]
        if cleanup_errors:
            report["passed"] = False
            report.setdefault("error", "Очистка опыта не подтверждена")
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    if not report["passed"]:
        raise SystemExit(report.get("error", "Эксперимент не прошёл"))
    print(f"Восстановление подтверждено, MTTR {report['mttr_seconds']:.1f} с. Результат: {args.output}")


if __name__ == "__main__":
    main()
