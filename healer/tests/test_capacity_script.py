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


@pytest.mark.parametrize("continue_series,expected_stages", [(False, 2), (True, 3)])
def test_cpu_calibration_can_continue_after_failure_and_keeps_first_boundary(monkeypatch, tmp_path, continue_series, expected_stages):
    objects, target, commands = controlled_capacity(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "argv", ["capacity", "--rates", "1,2,3", "--replicas", "2", "--duration", "90",
                                     "--output", str(target)] + (["--continue-after-failure"] if continue_series else []))
    counter = {"jobs": 0}

    class Deployment:
        changed = unknown = False
        def __init__(self, name, namespace, original, marker):
            self.name = name
        def mutate(self, replicas):
            objects[self.name]["spec"]["replicas"] = replicas
        def cleanup(self):
            pass

    class Job:
        kind, name, namespace, generation = "Job", "k6-spike", "demo", 1
        def __init__(self, manifest, marker):
            counter["jobs"] += 1
            self.uid, self.reads = "job-" + str(counter["jobs"]), 0
        def create(self, **kwargs):
            pass
        def current(self):
            self.reads += 1
            return {"metadata": {"uid": self.uid}, "status": {"active": 1} if self.reads <= 6 else {"succeeded": 1}}
        def cleanup(self):
            pass

    monkeypatch.setattr(capacity, "OwnedDeployment", Deployment)
    monkeypatch.setattr(capacity, "OwnedResource", Job)
    monkeypatch.setattr(capacity.time, "sleep", lambda *args: None)
    monkeypatch.setattr(capacity, "metrics", lambda *args: {"p95": .05, "error_ratio": 0, "request_rate": 1,
        "synthetic_success": 1, "synthetic_time": 100, "time": 110, "rollout_ready": True})
    monkeypatch.setattr(capacity, "scalar", lambda *args: 1)
    monkeypatch.setattr(capacity, "cmd", lambda *args, **kwargs: "current" if args[0] == "git" else
        json.dumps({"spec": {"template": {"spec": {"containers": [{"env": []}]}}}}) if "create" in args else json.dumps({"items": []}))
    monkeypatch.setattr(capacity, "job_summary", lambda *args: {"http_reqs": {"count": counter["jobs"] * 90},
        "dropped_iterations": {"count": 0}, "http_req_failed": {"rate": 0},
        "http_req_duration": {"p(95)": 50 if counter["jobs"] == 1 else 500}})

    capacity.main()
    report = json.loads(target.read_text(encoding="utf-8"))
    assert report["passed"] is True and len(report["stages"]) == expected_stages
    assert report["bounds"]["2"] == {"highest_tested_passing_rps": 1, "first_tested_failing_rps": 2}
    assert all(row["load_active"] is True for row in report["stages"][0]["samples"][:3])
    assert report["stages"][0]["samples"][-1]["load_active"] is False
