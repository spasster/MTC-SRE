"""Образ и шаблон проверяются после применения; чужое изменение и неопределенность сохраняются."""

import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
spec = importlib.util.spec_from_file_location("local_images", Path(__file__).resolve().parents[2] / "scripts/local-images.py")
images = importlib.util.module_from_spec(spec)
spec.loader.exec_module(images)

IDENTITY = "sha256:" + "a" * 64
TAG = "payment:local-" + "a" * 64


def object_for(image="payment:dev", generation=2):
    return {"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "payment", "namespace": "demo", "uid": "own-uid", "resourceVersion": "12", "generation": generation,
                         "annotations": {"deployment.kubernetes.io/revision": "2"}},
            "spec": {"replicas": 2, "template": {"metadata": {"labels": {"app": "payment"}},
                     "spec": {"containers": [{"name": "payment", "image": image}]}}},
            "status": {"observedGeneration": generation, "replicas": 2, "updatedReplicas": 2, "readyReplicas": 2, "availableReplicas": 2}}


@pytest.mark.parametrize("field,value", [("uid", "another-uid"), ("generation", 3), ("spec", {"replicas": 0})])
def test_known_conflict_preserves_operator_change(monkeypatch, field, value):
    original = object_for()
    changed = copy.deepcopy(original)
    changed["spec"] = value if field == "spec" else changed["spec"]
    if field != "spec":
        changed["metadata"][field] = value
    calls = []
    monkeypatch.setattr(images, "run_command", lambda *args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(
        returncode=1, stdout="", stderr="Error from server (Conflict)"))
    monkeypatch.setattr(images, "read_deployment", lambda *args: changed)
    with pytest.raises(RuntimeError, match="изменен другим"):
        images.apply_deployment(object_for(TAG), original)
    assert len(calls) == 1
    sent = json.loads(calls[0][1]["input_text"])
    assert sent["metadata"]["uid"] == "own-uid"
    assert sent["metadata"]["resourceVersion"] == "12"


def test_known_status_conflict_retries_with_new_version(monkeypatch):
    original, updated = object_for(), object_for()
    updated["metadata"]["resourceVersion"] = "13"
    accepted = object_for(TAG, generation=3)
    sent = []

    def run(*args, **kwargs):
        sent.append(json.loads(kwargs["input_text"]))
        return (SimpleNamespace(returncode=1, stdout="", stderr="Error from server (Conflict)") if len(sent) == 1
                else SimpleNamespace(returncode=0, stdout=json.dumps(accepted), stderr=""))

    monkeypatch.setattr(images, "run_command", run)
    monkeypatch.setattr(images, "read_deployment", lambda *args: updated)
    assert images.apply_deployment(object_for(TAG), original)["spec"]["template"] == accepted["spec"]["template"]
    assert [row["metadata"]["resourceVersion"] for row in sent] == ["12", "13"]


@pytest.mark.parametrize("response", [SimpleNamespace(returncode=1, stdout="", stderr="неизвестный ответ"),
                                      SimpleNamespace(returncode=0, stdout="not-json", stderr=""),
                                      SimpleNamespace(returncode=0, stdout=json.dumps(object_for()), stderr="")])
def test_unknown_or_unconfirmed_response_is_not_repeated(monkeypatch, response):
    calls = []
    monkeypatch.setattr(images, "run_command", lambda *args, **kwargs: calls.append(args) or response)
    with pytest.raises(RuntimeError):
        images.apply_deployment(object_for(TAG), object_for())
    assert len(calls) == 1


def runtime(monkeypatch, image_id=IDENTITY, source=None, changed_uid=False, controller=True, kind="ReplicaSet"):
    accepted = object_for(TAG)
    pod = {"metadata": {"name": "payment-pod", "uid": "pod-uid", "ownerReferences": [{"uid": "rs-uid", "controller": controller, "kind": kind}]},
           "status": {"containerStatuses": [{"name": "payment", "ready": True, "imageID": "containerd://" + image_id}]}}
    another = copy.deepcopy(pod)
    another["metadata"]["name"], another["metadata"]["uid"] = "payment-pod-2", "pod-uid-2"

    def cmd(*args, **kwargs):
        if args[:3] == ("kubectl", "rollout", "status"):
            return "ready"
        if args[:3] == ("kubectl", "get", "replicasets"):
            return json.dumps({"items": [{"metadata": {"uid": "rs-uid", "ownerReferences": [{"uid": "own-uid", "controller": True, "kind": "Deployment"}],
                "annotations": {"deployment.kubernetes.io/revision": "2"}}}]})
        if args[:3] == ("kubectl", "get", "pods"):
            return json.dumps({"items": [pod, another]})
        if args[:2] == ("kubectl", "exec"):
            return json.dumps(source or {"app/main.py": "expected-sha"})
        if args[:3] == ("kubectl", "get", "pod"):
            current = copy.deepcopy(pod if args[3] == "payment-pod" else another)
            if changed_uid:
                current["metadata"]["uid"] = "replacement"
            return json.dumps(current)
        raise AssertionError(args)

    monkeypatch.setattr(images, "cmd", cmd)
    monkeypatch.setattr(images, "deployment", lambda *args: accepted)
    return accepted


@pytest.mark.parametrize("option", [{"image_id": "sha256:" + "b" * 64}, {"source": {"app/main.py": "old-sha"}},
                                      {"changed_uid": True}, {"controller": False}, {"kind": "Deployment"}])
def test_ready_old_or_replaced_pod_does_not_pass(monkeypatch, option):
    accepted = runtime(monkeypatch, **option)
    with pytest.raises(RuntimeError):
        images.verify_runtime("payment", accepted, IDENTITY, TAG, {"app/main.py": "expected-sha"})


def test_all_replicas_require_image_and_source_identity(monkeypatch):
    accepted = runtime(monkeypatch)
    result = images.verify_runtime("payment", accepted, IDENTITY, TAG, {"app/main.py": "expected-sha"})
    assert result["passed"]
    assert {row["uid"] for row in result["pods"]} == {"pod-uid", "pod-uid-2"}


def test_reapply_same_immutable_image_has_same_template(monkeypatch):
    original = object_for(TAG)
    applied = []
    monkeypatch.setattr(images, "run_command", lambda *args, **kwargs: applied.append(json.loads(kwargs["input_text"])) or
                        SimpleNamespace(returncode=0, stdout=json.dumps(original), stderr=""))
    assert images.apply_deployment(object_for(TAG), original)["metadata"]["generation"] == 2
    assert applied == []


def test_server_defaults_are_accepted_but_changed_requested_field_is_not(monkeypatch):
    result = object_for(TAG)
    result["spec"]["template"]["spec"]["dnsPolicy"] = "ClusterFirst"
    monkeypatch.setattr(images, "run_command", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr=""))
    assert images.apply_deployment(object_for(TAG), object_for())["spec"]["template"]["spec"]["dnsPolicy"] == "ClusterFirst"
    result["spec"]["template"]["spec"]["containers"][0]["image"] = "operator:image"
    with pytest.raises(RuntimeError, match="Ответ API"):
        images.apply_deployment(object_for(TAG), object_for())


def test_failed_validation_keeps_unsuccessful_json(monkeypatch, tmp_path):
    target = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["images", "--component", "payment", "--output", str(target)])
    monkeypatch.setattr(images, "cmd", lambda *args, **kwargs: "")
    monkeypatch.setattr(images, "refresh", lambda *args: (_ for _ in ()).throw(RuntimeError("закрытые данные внешней команды")))
    with pytest.raises(SystemExit):
        images.main()
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["passed"] is False
    assert "закрытые" not in target.read_text(encoding="utf-8")
