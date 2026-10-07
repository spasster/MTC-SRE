"""Отказ подготовки не дает серии мощности права менять чужие ресурсы."""
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import capacity
import experiment


def controlled_capacity(monkeypatch, tmp_path):
    objects = {name: {"metadata": {"uid": "original-" + name, "generation": 1, "resourceVersion": "10", "annotations": {}},
                      "spec": {"replicas": 2 if name == "checkout" else 1,
                               "template": {"spec": {"containers": [{"name": name, "image": name + ":local-controlled", "resources": {}}]}}}}
               for name in ("checkout", "healer", "k6-background")}
    target = tmp_path / "result.json"
    commands = []
    monkeypatch.setattr(sys, "argv", ["capacity", "--rates", "1", "--replicas", "2", "--output", str(target)])
    def read(name, namespace):
        return copy.deepcopy(objects[name])
    monkeypatch.setattr(capacity, "deployment", read)
    monkeypatch.setattr(experiment, "deployment", read)
    monkeypatch.setattr(capacity, "cmd", lambda *args, **kwargs: commands.append(args) or json.dumps({"items": []}))
    monkeypatch.setattr(capacity, "wait_incident_closed", lambda: {"incident_closed": True})
    return objects, target, commands


def test_capacity_rejected_precondition_does_not_write_during_cleanup(monkeypatch, tmp_path):
    objects, target, commands = controlled_capacity(monkeypatch, tmp_path)
    writes = []
    monkeypatch.setattr(experiment, "run_command", lambda *args, **kwargs: writes.append(args))
    def reject():
        objects["checkout"]["spec"]["replicas"] = 7
        raise RuntimeError("Предыдущий инцидент не закрыт")
    monkeypatch.setattr(capacity, "wait_incident_closed", reject)
    with pytest.raises(SystemExit, match="не закрыт"):
        capacity.main()
    assert objects["checkout"]["spec"]["replicas"] == 7 and writes == []
    assert not any("delete" in args or "scale" in args or "import" in args for args in commands)
    report = json.loads(target.read_text(encoding="utf-8"))
    assert not report["passed"] and report["resource_intents"] == [] and report["cleanup_errors"] == []


def test_capacity_unknown_first_write_preserves_operator_scale(monkeypatch, tmp_path):
    objects, target, commands = controlled_capacity(monkeypatch, tmp_path)
    writes = []
    def unknown(*args, **kwargs):
        writes.append(args)
        objects["checkout"]["spec"]["replicas"] = 7
        raise RuntimeError("Ответ первой записи потерян")
    monkeypatch.setattr(experiment, "run_command", unknown)
    with pytest.raises(SystemExit, match="потерян"):
        capacity.main()
    assert len(writes) == 1 and objects["checkout"]["spec"]["replicas"] == 7
    assert not any("delete" in args or "scale" in args or "import" in args for args in commands)
    report = json.loads(target.read_text(encoding="utf-8"))
    assert not report["passed"] and len(report["cleanup_errors"]) == 1
    assert report["cleanup_errors"][0]["operation"] == "deployment/heal/healer"


def job_owner():
    job = {"metadata": {"uid": "created-job"}}
    return SimpleNamespace(namespace="demo", name="k6-spike", current=lambda: job)


def test_capacity_does_not_read_logs_from_a_replaced_job(monkeypatch):
    calls = []
    def response(*args, **kwargs):
        calls.append(args)
        return json.dumps({"items": [{"metadata": {"name": "foreign-pod", "ownerReferences": [
            {"kind": "Job", "uid": "replacement-job", "controller": True}]}}]})
    monkeypatch.setattr(capacity, "cmd", response)
    with pytest.raises(RuntimeError, match="итог"):
        capacity.job_summary(job_owner())
    assert not any("logs" in args for args in calls)


def test_capacity_reads_one_summary_from_its_own_job(monkeypatch):
    def response(*args, **kwargs):
        if "logs" in args:
            assert "owned-pod" in args and kwargs["timeout"] == 30
            return 'MTC_SUMMARY {"http_reqs":{"count":90}}\n'
        return json.dumps({"items": [{"metadata": {"name": "owned-pod", "ownerReferences": [
            {"kind": "Job", "uid": "created-job", "controller": True}]}}]})
    monkeypatch.setattr(capacity, "cmd", response)
    assert capacity.job_summary(job_owner())["http_reqs"]["count"] == 90


def test_capacity_rejects_job_replacement_after_log_read(monkeypatch):
    reads = iter([{"metadata": {"uid": "created-job"}}, {"metadata": {"uid": "replacement-job"}}])
    owner = job_owner()
    owner.current = lambda: next(reads)
    monkeypatch.setattr(capacity, "cmd", lambda *args, **kwargs: 'MTC_SUMMARY {"http_reqs":{"count":90}}\n'
        if "logs" in args else json.dumps({"items": [{"metadata": {"name": "owned-pod", "ownerReferences": [
            {"kind": "Job", "uid": "created-job", "controller": True}]}}]}))
    with pytest.raises(RuntimeError, match="не подтвержден"):
        capacity.job_summary(owner)
