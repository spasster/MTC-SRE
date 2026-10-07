"""Применить локальные образы по неизменному тегу и проверить код каждого рабочего пода."""

import argparse
import copy
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from experiment import cmd, deployment, run_command

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = {
    "checkout": ("checkout", "demo", "checkout", "apps/checkout", "k8s/checkout.yaml"),
    "payment": ("payment", "demo", "payment", "apps/payment", "k8s/payment.yaml"),
    "healer": ("healer", "heal", "healer", "healer", "k8s/healer.yaml"),
    "assistant": ("sre-assistant", "heal", "assistant", "healer", "k8s/assistant.yaml"),
}


def hashes(component):
    directory = ROOT / COMPONENTS[component][3]
    files = sorted((directory / "app").rglob("*.py")) + [directory / "requirements.txt"]
    if COMPONENTS[component][3] == "healer":
        files += [directory / "rules.yaml"] + sorted((directory / "prompts").glob("*"))
    return {str(path.relative_to(directory)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in files if path.is_file()}


def hash_command(expected):
    return ("import hashlib,json,pathlib; paths=" + repr(sorted(expected))
            + "; print(json.dumps({p:hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest() for p in paths}))")


def image(component, expected):
    name = "healer" if component == "assistant" else component
    identity = cmd("docker", "image", "inspect", name + ":dev", "--format", "{{.Id}}", timeout=20).strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", identity):
        raise RuntimeError("Docker не вернул однозначный SHA-256 образа")
    observed = json.loads(cmd("docker", "run", "--rm", "--entrypoint", "python", identity,
                              "-c", hash_command(expected), timeout=30))
    if observed != expected:
        raise RuntimeError("Собранный образ не соответствует исходникам; сначала выполните make images")
    return identity, name + ":local-" + identity.removeprefix("sha256:")


def container(obj, name):
    rows = [row for row in obj["spec"]["template"]["spec"]["containers"] if row["name"] == name]
    if len(rows) != 1:
        raise RuntimeError("Не найден единственный контейнер приложения")
    return rows[0]


def read_deployment(name, namespace):
    raw = cmd("kubectl", "get", "deployment", name, "-n", namespace, "--ignore-not-found", "-o", "json")
    return json.loads(raw) if raw.strip() else None


def unchanged(current, original):
    return (current is not None and current["metadata"]["uid"] == original["metadata"]["uid"]
            and current["metadata"]["generation"] == original["metadata"]["generation"]
            and current["spec"] == original["spec"])


def contains_fields(observed, requested):
    """API добавляет значения по умолчанию; запрошенные поля должны совпадать полностью."""
    if isinstance(requested, dict):
        return isinstance(observed, dict) and all(key in observed and contains_fields(observed[key], value) for key, value in requested.items())
    if isinstance(requested, list):
        return isinstance(observed, list) and len(observed) == len(requested) and all(contains_fields(a, b) for a, b in zip(observed, requested))
    return type(observed) is type(requested) and observed == requested


def apply_deployment(desired, original):
    """Повторяется только явно отклоненный CAS, после проверки исходного UID и шаблона."""
    name, namespace = desired["metadata"]["name"], desired["metadata"]["namespace"]
    if (original is not None and contains_fields(original["spec"], desired["spec"])
            and contains_fields(original["metadata"].get("labels", {}), desired["metadata"].get("labels", {}))):
        # resourceVersion в last-applied меняет generation даже без нового шаблона.
        return original
    current = original
    for _ in range(3):
        manifest = copy.deepcopy(desired)
        if original is not None:
            if not unchanged(current, original):
                raise RuntimeError("Deployment изменен другим участником; повтор записи запрещен")
            manifest["metadata"].update(uid=current["metadata"]["uid"], resourceVersion=current["metadata"]["resourceVersion"])
        response = run_command("kubectl", "create" if original is None else "apply", "-f", "-", "-o", "json",
                               input_text=json.dumps(manifest), timeout=30)
        if response.returncode:
            if original is not None and "Error from server (Conflict)" in response.stderr:
                current = read_deployment(name, namespace)
                continue
            raise RuntimeError("API не подтвердил применение Deployment; неизвестный результат не повторяется")
        try:
            result = json.loads(response.stdout)
            if (result["kind"] != "Deployment" or result["metadata"]["name"] != name
                    or result["metadata"]["namespace"] != namespace
                    or original is not None and result["metadata"]["uid"] != original["metadata"]["uid"]
                    or not contains_fields(result["spec"]["template"], desired["spec"]["template"])):
                raise ValueError("Неожиданный объект")
        except (ValueError, KeyError, TypeError):
            raise RuntimeError("Ответ API не подтвердил шаблон Deployment; повтор запрещен") from None
        return result
    raise RuntimeError("Три конфликта CAS; Deployment сохранен без дополнительной записи")


def controlled_by(obj, uid, kind):
    return any(row.get("uid") == uid and row.get("kind") == kind and row.get("controller") is True
               for row in obj["metadata"].get("ownerReferences", []))


def verify_runtime(component, accepted, identity, tag, expected):
    name, namespace, container_name, _, _ = COMPONENTS[component]
    cmd("kubectl", "rollout", "status", "deployment/" + name, "-n", namespace, "--timeout=180s", timeout=210)
    current = deployment(name, namespace)
    if not unchanged(current, accepted) or container(current, container_name)["image"] != tag:
        raise RuntimeError("Рабочий Deployment заменен или изменен после применения")
    replicas, status = current["spec"]["replicas"], current.get("status", {})
    if (replicas <= 0 or status.get("observedGeneration", 0) < current["metadata"]["generation"]
            or any(status.get(key, 0) != replicas for key in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas"))):
        raise RuntimeError("Не все реплики текущей ревизии готовы")
    revision = current["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision")
    if not revision:
        raise RuntimeError("Deployment не подтвердил текущую ревизию")
    sets = json.loads(cmd("kubectl", "get", "replicasets", "-n", namespace, "-o", "json"))["items"]
    owners = {row["metadata"]["uid"] for row in sets if controlled_by(row, current["metadata"]["uid"], "Deployment")
              and row["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision") == revision}
    if len(owners) != 1:
        raise RuntimeError("Не найден единственный ReplicaSet текущей ревизии")
    all_pods = json.loads(cmd("kubectl", "get", "pods", "-n", namespace, "-o", "json"))["items"]
    pods = [row for row in all_pods if not row["metadata"].get("deletionTimestamp")
            and any(controlled_by(row, uid, "ReplicaSet") for uid in owners)]
    if len(pods) != replicas:
        raise RuntimeError("Список подов не совпал с числом текущих реплик")
    verified = []
    for pod in pods:
        pod_name, uid = pod["metadata"]["name"], pod["metadata"]["uid"]
        statuses = [row for row in pod.get("status", {}).get("containerStatuses", []) if row["name"] == container_name]
        if (len(statuses) != 1 or statuses[0].get("ready") is not True
                or statuses[0].get("imageID", "").rsplit("sha256:", 1)[-1] != identity.removeprefix("sha256:")):
            raise RuntimeError("Готовый под использует другой образ приложения")
        observed = json.loads(cmd("kubectl", "exec", "-n", namespace, pod_name, "-c", container_name,
                                  "--", "python", "-c", hash_command(expected), timeout=30))
        after = json.loads(cmd("kubectl", "get", "pod", pod_name, "-n", namespace, "-o", "json"))
        if after["metadata"]["uid"] != uid or after["metadata"].get("deletionTimestamp") or observed != expected:
            raise RuntimeError("Код рабочего пода не совпал или под заменен во время проверки")
        verified.append({"pod": pod_name, "uid": uid, "image_id": statuses[0]["imageID"], "source_sha256": observed})
    final = deployment(name, namespace)
    final_status = final.get("status", {})
    if (not unchanged(final, current)
            or final["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision") != revision
            or final_status.get("observedGeneration", 0) < final["metadata"]["generation"]
            or any(final_status.get(key, 0) != replicas for key in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas"))):
        raise RuntimeError("Рабочий Deployment заменен, изменен или потерял готовность во время проверки реплик")
    return {"deployment_uid": current["metadata"]["uid"], "generation": current["metadata"]["generation"],
            "image_id": identity, "image_tag": tag, "pods": verified, "passed": True}


def refresh(component, cluster, verify_only=False):
    name, namespace, container_name, _, file_name = COMPONENTS[component]
    expected = hashes(component)
    identity, tag = image(component, expected)
    original = read_deployment(name, namespace)
    if verify_only:
        if original is None:
            raise RuntimeError("Deployment для проверки отсутствует")
        return verify_runtime(component, original, identity, tag, expected)
    if original is not None:
        existing_image = container(original, container_name)["image"]
        image_name = "healer" if component == "assistant" else component
        if existing_image != image_name + ":dev" and not re.fullmatch(re.escape(image_name) + r":local-[0-9a-f]{64}", existing_image):
            raise RuntimeError("Deployment использует отдельный образ оператора; автоматическая замена запрещена")
    manifests = list(yaml.safe_load_all((ROOT / file_name).read_text(encoding="utf-8")))
    selected = [row for row in manifests if row and row.get("kind") == "Deployment"]
    if len(selected) != 1 or any(selected[0]["metadata"].get(key) != value for key, value in (("name", name), ("namespace", namespace))):
        raise RuntimeError("Манифест не определяет единственный ожидаемый Deployment")
    desired = selected[0]
    container(desired, container_name)["image"] = tag
    cmd("docker", "tag", identity, tag, timeout=20)
    cmd("k3d", "image", "import", tag, "-c", cluster, "--mode", "direct", timeout=180)
    for row in manifests:
        if row and row is not desired:
            cmd("kubectl", "apply", "-f", "-", input_text=json.dumps(row), timeout=30)
    accepted = apply_deployment(desired, original)
    return verify_runtime(component, accepted, identity, tag, expected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component", nargs="+", required=True, choices=COMPONENTS)
    parser.add_argument("--cluster", default=os.getenv("CLUSTER", "mtc"))
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "passed": False, "components": {},
              "verifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    try:
        cmd("sh", str(ROOT / "scripts/require-context.sh"), args.cluster)
        for component in args.component:
            report["components"][component] = refresh(component, args.cluster, args.verify_only)
        report["passed"] = True
    except Exception as error:
        report["error_type"] = type(error).__name__
        # Ошибки собственных проверок объясняются, внешние ответы и аргументы не публикуются.
        report["error"] = str(error) if str(error).startswith(("Deployment", "Docker", "Собранный", "API", "Ответ API", "Рабочий",
            "Не все", "Не найден", "Список подов", "Готовый", "Код рабочего", "Манифест", "Три конфликта")) else "Внешняя команда не подтвердила результат"
    finally:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    if not report["passed"]:
        raise SystemExit(report["error"])
    print("Проверены неизменные образы и код всех текущих реплик", flush=True)


if __name__ == "__main__":
    main()
