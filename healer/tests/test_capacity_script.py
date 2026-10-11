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


@pytest.mark.parametrize("continue_series,expected_stages,baseline_ok", [(False, 2, True), (True, 3, True), (True, 3, False)])
def test_cpu_calibration_can_continue_after_failure_and_keeps_first_boundary(monkeypatch, tmp_path, continue_series, expected_stages, baseline_ok):
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
            self.changed = True
        def cleanup(self):
            self.changed = False

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
    def confirmed_baseline(owners, report):
        if not baseline_ok:
            raise RuntimeError("Базовый SLO не подтвержден")
        report["baseline_restored"] = {"passed": True}
    monkeypatch.setattr(capacity, "confirm_baseline", confirmed_baseline)
    def restored_checkout(owner):
        return {"attempted": True, "confirmed_zero": True}
    monkeypatch.setattr(capacity, "reset_checkout_for_baseline", restored_checkout)
    monkeypatch.setattr(capacity.time, "sleep", lambda *args: None)
    monkeypatch.setattr(capacity, "metrics", lambda *args: {"p95": .05, "error_ratio": 0, "request_rate": 1,
        "synthetic_success": 1, "synthetic_time": 100, "time": 110, "rollout_ready": True})
    monkeypatch.setattr(capacity, "scalar", lambda *args: 1)
    monkeypatch.setattr(capacity, "cmd", lambda *args, **kwargs: "current" if args[0] == "git" else
        json.dumps({"spec": {"template": {"spec": {"containers": [{"env": []}]}}}}) if "create" in args else json.dumps({"items": []}))
    monkeypatch.setattr(capacity, "job_summary", lambda *args: {"http_reqs": {"count": counter["jobs"] * 90},
        "dropped_iterations": {"count": 0}, "http_req_failed": {"rate": 0},
        "http_req_duration": {"p(95)": 50 if counter["jobs"] == 1 else 500}})

    if baseline_ok:
        capacity.main()
    else:
        with pytest.raises(SystemExit, match="Базовый контур"):
            capacity.main()
    report = json.loads(target.read_text(encoding="utf-8"))
    assert report["passed"] is baseline_ok and len(report["stages"]) == expected_stages
    assert report["bounds"]["2"] == {"highest_tested_passing_rps": 1, "first_tested_failing_rps": 2}
    assert all(row["load_active"] is True for row in report["stages"][0]["samples"][:3])
    assert report["stages"][0]["samples"][-1]["load_active"] is False


def baseline_owner(monkeypatch):
    original = {"metadata": {"uid": "own-checkout", "generation": 1, "annotations": {}},
                "spec": {"replicas": 2, "template": {"spec": {"containers": []}}},
                "status": {"replicas": 2, "updatedReplicas": 2, "readyReplicas": 2, "availableReplicas": 2,
                           "observedGeneration": 1}}
    owner = experiment.OwnedDeployment("checkout", "demo", copy.deepcopy(original), "owned-series")
    owner.changed = True
    owner.expected = copy.deepcopy(original)
    owner.expected["metadata"]["annotations"][experiment.OWNER_ANNOTATION] = owner.marker
    writes = []
    def mutate(replicas, **kwargs):
        writes.append(replicas)
        owner.expected["spec"]["replicas"] = replicas
        owner.expected["metadata"]["generation"] += 1
        owner.expected["status"] = {"replicas": replicas, "observedGeneration": owner.expected["metadata"]["generation"]}
    def cleanup():
        mutate(2)
        owner.changed = False
        owner.expected["metadata"]["annotations"] = {}
    monkeypatch.setattr(owner, "mutate", mutate)
    monkeypatch.setattr(owner, "cleanup", cleanup)
    monkeypatch.setattr(capacity, "deployment", lambda *args: copy.deepcopy(owner.expected))
    monkeypatch.setattr(capacity, "cmd", lambda *args, **kwargs: json.dumps({"items": []}))
    return owner, writes


def test_capacity_baseline_waits_for_stopped_processes_before_restore(monkeypatch):
    owner, writes = baseline_owner(monkeypatch)
    pending = iter([[{"metadata": {"uid": "old-pod"}}], []])
    monkeypatch.setattr(capacity, "cmd", lambda *args, **kwargs: json.dumps({"items": next(pending)}) if "get" in args else "")
    monkeypatch.setattr(capacity.time, "sleep", lambda *args: None)
    result = capacity.reset_checkout_for_baseline(owner)
    assert writes == [0, 2] and result["confirmed_zero"] is True
    assert owner.changed is False and owner.expected["spec"] == owner.original["spec"]


def test_capacity_baseline_preserves_foreign_scale_during_wait(monkeypatch):
    owner, writes = baseline_owner(monkeypatch)
    def foreign(*args):
        obj = copy.deepcopy(owner.expected)
        obj["spec"]["replicas"] = 7
        return obj
    monkeypatch.setattr(capacity, "deployment", foreign)
    with pytest.raises(RuntimeError, match="другим участником"):
        capacity.reset_checkout_for_baseline(owner)
    assert writes == [0] and owner.changed is True


def test_capacity_baseline_unknown_result_does_not_restart(monkeypatch):
    owner, writes = baseline_owner(monkeypatch)
    owner.unknown = True
    with pytest.raises(RuntimeError, match="Владение"):
        capacity.reset_checkout_for_baseline(owner)
    assert writes == []


def test_capacity_baseline_cannot_accept_zero_read_after_deadline(monkeypatch):
    owner, writes = baseline_owner(monkeypatch)
    clock = [0]
    monkeypatch.setattr(capacity.time, "monotonic", lambda: clock[0])
    def late(*args, **kwargs):
        clock[0] = 6
        return json.dumps({"items": []})
    monkeypatch.setattr(capacity, "cmd", late)
    with pytest.raises(RuntimeError, match="не подтверждено"):
        capacity.reset_checkout_for_baseline(owner, timeout=5)
    assert writes == [0]


def restored_baseline(monkeypatch):
    owners, objects = {}, {}
    for namespace, name, replicas in [("demo", "checkout", 2), ("demo", "k6-background", 1), ("heal", "healer", 1)]:
        obj = {"metadata": {"uid": "own-" + name, "generation": 2, "annotations": {}}, "spec": {"replicas": replicas},
               "status": {"replicas": replicas, "updatedReplicas": replicas, "readyReplicas": replicas,
                          "availableReplicas": replicas, "observedGeneration": 2}}
        owners[(namespace, name)] = SimpleNamespace(original=copy.deepcopy(obj), expected=copy.deepcopy(obj), changed=False, unknown=False)
        objects[(namespace, name)] = obj
    monkeypatch.setattr(capacity, "deployment", lambda name, namespace: copy.deepcopy(objects[(namespace, name)]))
    monkeypatch.setattr(capacity, "cmd", lambda *args, **kwargs: "")
    monkeypatch.setattr(capacity, "wait_healthy", lambda *args: [{"slo_healthy": True}])
    monkeypatch.setattr(capacity, "wait_incident_closed", lambda **kwargs: {"incident_closed": True})
    return owners, objects


def test_capacity_confirmed_baseline_keeps_health_and_closed_incident(monkeypatch):
    owners, objects = restored_baseline(monkeypatch)
    report = {}
    capacity.confirm_baseline(owners, report)
    assert report["baseline_restored"]["passed"] is True and report["baseline_restored"]["health_checked"] is True
    assert len(report["baseline_restored"]["components"]) == 3
    assert report["baseline_health_samples"] == [{"slo_healthy": True}]
    assert report["baseline_incident"]["incident_closed"] is True


def test_capacity_baseline_rechecks_operator_change_after_healthy_read(monkeypatch):
    owners, objects = restored_baseline(monkeypatch)
    def changed(*args):
        objects[("demo", "checkout")]["spec"]["replicas"] = 7
        objects[("demo", "checkout")]["metadata"]["generation"] = 3
        return [{"slo_healthy": True}]
    monkeypatch.setattr(capacity, "wait_healthy", changed)
    report = {}
    with pytest.raises(RuntimeError, match="не подтверждены"):
        capacity.confirm_baseline(owners, report)
    assert "baseline_restored" not in report and objects[("demo", "checkout")]["spec"]["replicas"] == 7


def test_capacity_failed_baseline_keeps_precondition_samples(monkeypatch):
    owners, objects = restored_baseline(monkeypatch)
    def unhealthy(*args):
        raise experiment.InitialHealthError([{"p95": 2, "slo_healthy": False}])
    monkeypatch.setattr(capacity, "wait_healthy", unhealthy)
    report = {}
    with pytest.raises(experiment.InitialHealthError):
        capacity.confirm_baseline(owners, report)
    assert report["baseline_health_samples"] == [{"p95": 2, "slo_healthy": False}]
    assert "baseline_restored" not in report


def refinement_grid():
    path = Path(__file__).resolve().parents[1] / "tests/fixtures/cpu-capacity-ci-gap.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_refinement_measures_both_replica_counts_and_stops_on_a_proven_pair(monkeypatch):
    report, calls = refinement_grid(), []
    owner = SimpleNamespace(mutate=lambda replicas: calls.append(("scale", replicas)))
    monkeypatch.setattr(capacity, "cmd", lambda *args, **kwargs: "")
    def measure(args, result, jobs, marker, replicas, rate):
        calls.append(("measure", replicas, rate))
        sample = {"time": 110, "synthetic_time": 100, "synthetic_success": 1, "error_ratio": 0,
                  "request_rate": rate, "load_active": True, "rollout_ready": True,
                  "p95": .6 if replicas == 2 else .1, "cpu_throttle": .7 if replicas == 2 else .2}
        result["stages"].append({"replicas": replicas, "planned_rps": rate, "passed": replicas == 5,
            "sent": rate * args.duration, "dropped": 0, "client_p95_ms": 600 if replicas == 2 else 100,
            "client": {"http_req_failed": {"rate": 0}}, "samples": [{**sample, "time": 110 + i * 10} for i in range(3)]})
    monkeypatch.setattr(capacity, "measure_stage", measure)
    capacity.refine_profile(SimpleNamespace(duration=120), report, {("demo", "checkout"): owner}, [], "owned")
    assert calls == [("scale", 2), ("measure", 2, 21), ("scale", 5), ("measure", 5, 21)]
    assert report["cpu_refinement"] == {"max_additional_rates": 3, "attempted_rates": [21], "profile_found": True, "selected_rps": 21}


def test_refinement_has_a_finite_budget_and_keeps_failed_measurements(monkeypatch):
    report, calls = refinement_grid(), []
    # Широкий интервал нужен, чтобы доказать остановку именно по бюджету, а не исчерпанию сетки.
    report["stages"] = [row for row in report["stages"] if row["replicas"] == 2 and row["planned_rps"] in (3, 24)]
    report["stages"][1]["planned_rps"] = 100
    owner = SimpleNamespace(mutate=lambda replicas: calls.append(replicas))
    monkeypatch.setattr(capacity, "cmd", lambda *args, **kwargs: "")
    def incomplete(args, result, jobs, marker, replicas, rate):
        result["stages"].append({"replicas": replicas, "planned_rps": rate, "passed": False,
            "sent": rate * args.duration, "dropped": 1, "client_p95_ms": 500,
            "client": {"http_req_failed": {"rate": 0}}, "samples": []})
    monkeypatch.setattr(capacity, "measure_stage", incomplete)
    with pytest.raises(RuntimeError, match="не подтвержден"):
        capacity.refine_profile(SimpleNamespace(duration=120), report, {("demo", "checkout"): owner}, [], "owned")
    assert len(report["cpu_refinement"]["attempted_rates"]) == 3 and calls == [2, 5, 2, 5, 2, 5]
    assert report["cpu_refinement"]["profile_found"] is False
    assert len(report["stages"]) == 8 and all(row["dropped"] == 1 for row in report["stages"][2:])


@pytest.mark.parametrize("arguments", [["--replicas", "2", "--continue-after-failure"], ["--replicas", "2,5"]])
def test_refinement_rejects_an_unpaired_or_incomplete_plan_before_any_write(monkeypatch, arguments):
    monkeypatch.setattr(sys, "argv", ["capacity", "--refine-cpu-profile", *arguments])
    calls = []
    monkeypatch.setattr(capacity, "cmd", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(SystemExit) as error:
        capacity.main()
    assert error.value.code == 2 and calls == []
