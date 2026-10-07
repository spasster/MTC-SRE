"""Новый опыт допускается только после закрытого инцидента."""

import json
import ast
import copy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import experiment
from app import main


@pytest.mark.parametrize("key,value", [
    ("_actions", 1), ("_actions", False), ("_broke_at", 100), ("_fixed_at", 120),
    ("_pending", {"action": "rollback"}), ("_action_intent", {"status": "unknown"}),
    ("_uncertain", True), ("_recovery_pending", True), ("_resolved_pending", True),
    ("_scaled_by_healer", True), ("_scale_down_pending", True), ("_closed", ["redis"]),
])
def test_active_or_incomplete_incident_blocks_new_experiment(key, value):
    main.reset_state()
    state = main.state_snapshot()
    state[key] = value
    assert not experiment.closed_incident(state)


def test_wait_accepts_only_closed_saved_state(monkeypatch):
    main.reset_state()
    clean = main.state_snapshot()
    active = {**clean, "_broke_at": 100}
    states = iter([active, clean])
    monkeypatch.setattr(experiment, "cmd", lambda *args, **kwargs: json.dumps({"data": {"state.json": json.dumps({"state": next(states)})}}))
    monkeypatch.setattr(experiment.time, "sleep", lambda duration: None)
    assert experiment.wait_incident_closed()["incident_closed"]
    assert not experiment.closed_incident({})


def test_guard_timeout_prevents_fault_injection(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(sys, "argv", ["experiment", "--mode", "baseline", "--output", str(tmp_path / "result.json")])
    monkeypatch.setattr(experiment, "cmd", lambda *args, **kwargs: commands.append(args) or json.dumps({"items": []}))
    monkeypatch.setattr(experiment, "deployment", lambda name, namespace: {"spec": {"replicas": 2 if name == "checkout" else 1}})
    monkeypatch.setattr(experiment, "wait_healthy", lambda *args: None)
    monkeypatch.setattr(experiment, "wait_incident_closed", lambda: (_ for _ in ()).throw(RuntimeError("Предыдущий инцидент не закрыт")))
    with pytest.raises(SystemExit, match="не закрыт"):
        experiment.main()
    assert not any(args[:3] == ("sh", "chaos/inject.sh", "slow-pod") for args in commands)
    assert not any(args[0] == "kubectl" and any(verb in args for verb in ("delete", "patch", "scale", "apply", "create")) for args in commands)
    assert not json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))["passed"]


def test_closed_read_after_deadline_cannot_start_an_experiment(monkeypatch):
    main.reset_state()
    clock = [0]
    budgets = []
    def delayed(*args, timeout):
        budgets.append(timeout)
        clock[0] = 6
        return json.dumps({"data": {"state.json": json.dumps({"state": main.state_snapshot()})}})
    monkeypatch.setattr(experiment.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(experiment, "cmd", delayed)
    with pytest.raises(RuntimeError, match="не закрыт"):
        experiment.wait_incident_closed(timeout=5)
    assert budgets == [5]


def test_stalled_process_is_terminated_without_echoing_arguments():
    with pytest.raises(RuntimeError, match="не завершилась") as failure:
        experiment.cmd(sys.executable, "-c", "import time; time.sleep(30)", "controlled-private-value", timeout=.1)
    assert "controlled-private-value" not in str(failure.value)


def test_cleanup_failure_keeps_a_failed_result(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["experiment", "--mode", "baseline", "--output", str(tmp_path / "result.json")])
    monkeypatch.setattr(experiment, "cmd", lambda *args, **kwargs: json.dumps({"items": []}))
    monkeypatch.setattr(experiment, "deployment", lambda name, namespace: {"spec": {"replicas": 2 if name == "checkout" else 1}})
    monkeypatch.setattr(experiment, "wait_healthy", lambda *args: None)
    calls = [0]
    def closed_once():
        calls[0] += 1
        if calls[0] == 1:
            return {"incident_closed": True}
        raise RuntimeError("Предыдущий инцидент не закрыт")
    monkeypatch.setattr(experiment, "wait_incident_closed", closed_once)
    monkeypatch.setattr(experiment.OwnedDeployment, "mutate", lambda self, *args, **kwargs: setattr(self, "changed", True))
    def failed_restore(self):
        if self.changed:
            raise RuntimeError("Очистка не подтверждена")
    monkeypatch.setattr(experiment.OwnedDeployment, "cleanup", failed_restore)
    with pytest.raises(SystemExit):
        experiment.main()
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert not result["passed"] and result["cleanup_errors"]
    assert "не закрыт" in result["error"]


def test_baseline_cleanup_imports_the_original_pinned_healer_image(monkeypatch, tmp_path):
    commands = []
    pinned = "healer:local-" + "a" * 64
    monkeypatch.setattr(sys, "argv", ["experiment", "--mode", "baseline", "--output", str(tmp_path / "result.json")])
    monkeypatch.setattr(experiment, "cmd", lambda *args, **kwargs: commands.append(args) or json.dumps({"items": []}))
    monkeypatch.setattr(experiment, "deployment", lambda name, namespace: {
        "spec": {"replicas": 2 if name == "checkout" else 1,
                 "template": {"spec": {"containers": [{"name": name, "image": pinned}]}}}})
    checks = [0]
    def healthy_once(*args, **kwargs):
        checks[0] += 1
        if checks[0] > 1:
            raise RuntimeError("Повторный допуск запрещен")
    monkeypatch.setattr(experiment, "wait_healthy", healthy_once)
    monkeypatch.setattr(experiment, "wait_incident_closed", lambda: {"incident_closed": True})
    monkeypatch.setattr(experiment.OwnedDeployment, "mutate", lambda self, *args, **kwargs: setattr(self, "changed", True))
    monkeypatch.setattr(experiment.OwnedDeployment, "cleanup", lambda self: None)
    with pytest.raises(SystemExit, match="Повторный допуск"):
        experiment.main()
    imports = [args for args in commands if args[:3] == ("k3d", "image", "import")]
    assert len(imports) == 1 and imports[0][3] == pinned
    assert not json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))["passed"]


def test_no_direct_child_process_can_bypass_the_deadline():
    tree = ast.parse(Path(experiment.__file__).read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
             and node.func.value.id == "subprocess" and node.func.attr == "run"]
    assert calls and all(any(keyword.arg == "timeout" for keyword in call.keywords) for call in calls)


def test_unknown_json_injection_is_not_repeated_or_disclosed(monkeypatch):
    calls = []
    def stalled(args, **kwargs):
        calls.append((args, kwargs))
        raise experiment.subprocess.TimeoutExpired(args, kwargs["timeout"], output="закрытые данные")
    monkeypatch.setattr(experiment.subprocess, "run", stalled)
    with pytest.raises(RuntimeError, match="не завершилась") as failure:
        experiment.cmd("kubectl", "apply", "-f", "-", input_text='{"private":"закрытые данные"}', timeout=12)
    assert len(calls) == 1 and calls[0][1]["timeout"] == 12
    assert "--request-timeout=10.000000s" in calls[0][0]
    assert "закрытые данные" not in str(failure.value)


def test_observability_timeout_has_a_finite_process_budget():
    with pytest.raises(RuntimeError, match="не завершилась"):
        experiment.run_command(sys.executable, "-c", "import time; time.sleep(30)", timeout=.1)


def controlled_deployment(monkeypatch):
    current = {"metadata": {"uid": "deployment-owned", "generation": 4, "resourceVersion": "10", "annotations": {}},
               "spec": {"replicas": 2, "template": {"metadata": {"annotations": {}}, "spec": {"containers": []}}}}
    writes = []
    monkeypatch.setattr(experiment, "deployment", lambda *args: copy.deepcopy(current))
    def patch(args, **kwargs):
        writes.append((args, kwargs))
        body = json.loads(args[args.index("-p") + 1])
        assert body["metadata"]["uid"] == current["metadata"]["uid"]
        assert body["metadata"]["resourceVersion"] == current["metadata"]["resourceVersion"]
        requested = copy.deepcopy(current["spec"])
        requested.update(body["spec"])
        if requested != current["spec"]:
            current["metadata"]["generation"] += 1
        current["spec"] = requested
        marker = body["metadata"]["annotations"][experiment.OWNER_ANNOTATION]
        if marker is None:
            current["metadata"]["annotations"].pop(experiment.OWNER_ANNOTATION, None)
        else:
            current["metadata"]["annotations"][experiment.OWNER_ANNOTATION] = marker
        current["metadata"]["resourceVersion"] = str(int(current["metadata"]["resourceVersion"]) + 1)
        return SimpleNamespace(returncode=0, stdout=json.dumps(current), stderr="")
    monkeypatch.setattr(experiment, "run_command", lambda *args, **kwargs: patch(args, **kwargs))
    return current, writes, patch


@pytest.mark.parametrize("foreign_change", ["replicas", "generation", "uid", "marker", "template"])
def test_cleanup_preserves_foreign_deployment_changes(monkeypatch, foreign_change):
    current, writes, _ = controlled_deployment(monkeypatch)
    owner = experiment.OwnedDeployment("checkout", "demo", copy.deepcopy(current), "this-run")
    owner.mutate(5)
    if foreign_change == "replicas":
        current["spec"]["replicas"] = 7
    elif foreign_change == "generation":
        current["metadata"]["generation"] += 2
    elif foreign_change == "uid":
        current["metadata"]["uid"] = "replacement"
    elif foreign_change == "marker":
        current["metadata"]["annotations"][experiment.OWNER_ANNOTATION] = "other-run"
    else:
        current["spec"]["template"]["spec"]["containers"] = [{"name": "operator"}]
    untouched = copy.deepcopy(current)
    with pytest.raises(RuntimeError, match="другим участником"):
        owner.cleanup()
    assert current == untouched and len(writes) == 1


def test_only_rejected_cas_can_be_retried(monkeypatch):
    current, writes, patch = controlled_deployment(monkeypatch)
    owner = experiment.OwnedDeployment("checkout", "demo", copy.deepcopy(current), "this-run")
    conflicts = []
    def conflict_once(*args, **kwargs):
        if not conflicts:
            conflicts.append(True)
            current["metadata"]["resourceVersion"] = "11"
            return SimpleNamespace(returncode=1, stdout="", stderr="Error from server (Conflict)")
        return patch(args, **kwargs)
    monkeypatch.setattr(experiment, "run_command", conflict_once)
    owner.mutate(5)
    owner.cleanup()
    assert conflicts == [True] and len(writes) == 2 and current["spec"]["replicas"] == 2
    assert experiment.OWNER_ANNOTATION not in current["metadata"]["annotations"]


def test_restart_requires_expected_template_in_api_response(monkeypatch):
    current, _, _ = controlled_deployment(monkeypatch)
    owner = experiment.OwnedDeployment("checkout", "demo", copy.deepcopy(current), "this-run")
    reported = copy.deepcopy(current)
    reported["metadata"]["annotations"][experiment.OWNER_ANNOTATION] = "this-run"
    # API не подтвердил запрошенную отметку в шаблоне пода.
    monkeypatch.setattr(experiment, "run_command", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(reported), stderr=""))
    with pytest.raises(RuntimeError, match="Ответ API"):
        owner.mutate(2, restart=True)
    assert owner.unknown
    with pytest.raises(RuntimeError, match="неизвестен"):
        owner.cleanup()


def test_unknown_scale_cannot_be_repeated_by_cleanup(monkeypatch):
    current, writes, _ = controlled_deployment(monkeypatch)
    owner = experiment.OwnedDeployment("checkout", "demo", copy.deepcopy(current), "this-run")
    calls = []
    def unknown(*args, **kwargs):
        calls.append(True)
        raise RuntimeError("Ответ API не получен")
    monkeypatch.setattr(experiment, "run_command", unknown)
    with pytest.raises(RuntimeError, match="не получен"):
        owner.mutate(5)
    with pytest.raises(RuntimeError, match="неизвестен"):
        owner.cleanup()
    assert calls == [True] and not writes


def test_owned_test_release_restores_the_exact_template_and_other_environment(monkeypatch):
    current, writes, _ = controlled_deployment(monkeypatch)
    current["spec"]["template"]["spec"]["containers"] = [
        {"name": "checkout", "image": "checkout:local-controlled", "env": [
            {"name": "KEEP", "value": "unchanged"}, {"name": "FAIL_RATE", "value": "0"}]},
        {"name": "other", "image": "other:controlled"}]
    original = copy.deepcopy(current)
    owner = experiment.OwnedDeployment("checkout", "demo", original, "this-run")
    owner.mutate(2, environment={"FAIL_RATE": "1"})
    assert current["spec"]["template"]["spec"]["containers"][0]["env"] == [
        {"name": "KEEP", "value": "unchanged"}, {"name": "FAIL_RATE", "value": "1"}]
    owner.cleanup(restore_template=True)
    assert current["spec"] == original["spec"] and len(writes) == 2
    assert experiment.OWNER_ANNOTATION not in current["metadata"]["annotations"]


def test_unknown_test_release_response_cannot_trigger_an_undo(monkeypatch):
    current, writes, _ = controlled_deployment(monkeypatch)
    current["spec"]["template"]["spec"]["containers"] = [{"name": "checkout", "env": []}]
    owner = experiment.OwnedDeployment("checkout", "demo", copy.deepcopy(current), "this-run")
    calls = []
    def unknown(*args, **kwargs):
        calls.append(True)
        current["metadata"]["uid"] = "operator-replacement"
        raise RuntimeError("Ответ тестового релиза потерян")
    monkeypatch.setattr(experiment, "run_command", unknown)
    with pytest.raises(RuntimeError, match="потерян"):
        owner.mutate(2, environment={"FAIL_RATE": "1"})
    with pytest.raises(RuntimeError, match="неизвестен"):
        owner.cleanup(restore_template=True)
    assert calls == [True] and not writes and current["metadata"]["uid"] == "operator-replacement"


@pytest.mark.parametrize("change", ["uid", "generation", "marker"])
def test_created_resource_cleanup_preserves_a_foreign_object(monkeypatch, change):
    manifest = {"kind": "NetworkChaos", "metadata": {"name": "checkout-slow-pod", "namespace": "demo"}, "spec": {}}
    owner = experiment.OwnedResource(manifest, "this-run")
    owner.uid, owner.generation = "own-chaos", 1
    current = {"metadata": {"uid": "own-chaos", "generation": 1, "resourceVersion": "12", "annotations": {
        experiment.OWNER_ANNOTATION: "this-run"}}}
    if change == "uid":
        current["metadata"]["uid"] = "recreated-chaos"
    elif change == "generation":
        current["metadata"]["generation"] = 2
    else:
        current["metadata"]["annotations"][experiment.OWNER_ANNOTATION] = "other-run"
    monkeypatch.setattr(experiment, "resource", lambda *args: current)
    monkeypatch.setattr(experiment, "cmd", lambda *args, **kwargs: pytest.fail("Чужой объект нельзя удалять"))
    with pytest.raises(RuntimeError, match="очистка запрещена"):
        owner.cleanup()


def test_unknown_create_is_reconciled_by_marker_without_a_second_post(monkeypatch):
    owner = experiment.OwnedResource({"kind": "Job", "metadata": {"name": "k6-spike", "namespace": "demo"}, "spec": {}}, "this-run")
    observed = {"metadata": {"uid": "own-job", "generation": 1, "resourceVersion": "17", "annotations": {
        experiment.OWNER_ANNOTATION: "this-run"}}}
    commands = []
    def command(*args, **kwargs):
        commands.append((args, kwargs))
        if "create" in args:
            raise RuntimeError("Ответ API не получен")
    monkeypatch.setattr(experiment, "cmd", command)
    def delete(*args, **kwargs):
        commands.append((args, kwargs))
        options = json.loads(kwargs["input_text"])
        assert options["preconditions"] == {"uid": "own-job", "resourceVersion": "17"}
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(experiment, "run_command", delete)
    reads = iter([observed, None])
    monkeypatch.setattr(experiment, "resource", lambda *args: next(reads))
    with pytest.raises(RuntimeError, match="не получен"):
        owner.create(timeout=12)
    owner.cleanup()
    assert len(commands) == 2 and sum("create" in args for args, _ in commands) == 1
    assert "--raw=/apis/batch/v1/namespaces/demo/jobs/k6-spike" in commands[1][0]


def test_delete_conflict_rechecks_resource_ownership_before_retry(monkeypatch):
    owner = experiment.OwnedResource({"kind": "Job", "metadata": {"name": "k6-spike", "namespace": "demo"}, "spec": {}}, "this-run")
    owner.uid, owner.generation = "own-job", 1
    original = {"metadata": {"uid": "own-job", "generation": 1, "resourceVersion": "10", "annotations": {
        experiment.OWNER_ANNOTATION: "this-run"}}}
    replacement = copy.deepcopy(original)
    replacement["metadata"]["uid"] = "other-job"
    reads = iter([original, replacement])
    monkeypatch.setattr(experiment, "resource", lambda *args: next(reads))
    writes = []
    def rejected(*args, **kwargs):
        writes.append(True)
        return SimpleNamespace(returncode=1, stdout="", stderr="Error from server (Conflict)")
    monkeypatch.setattr(experiment, "run_command", rejected)
    with pytest.raises(RuntimeError, match="очистка запрещена"):
        owner.cleanup()
    assert writes == [True]


@pytest.mark.parametrize("kind", ["NetworkChaos", "PodChaos", "StressChaos"])
def test_chaos_status_generation_does_not_block_owned_cleanup(monkeypatch, kind):
    owner = experiment.OwnedResource({"kind": kind, "metadata": {"name": "checkout-slow-pod", "namespace": "demo"},
                                     "spec": {"selector": {"pods": {"demo": ["selected"]}}}}, "this-run")
    created = {"metadata": {"uid": "own-chaos", "generation": 1, "resourceVersion": "10",
                            "annotations": {experiment.OWNER_ANNOTATION: "this-run"}},
               "spec": {"selector": {"pods": {"demo": ["selected"]}, "namespaces": ["demo"]}}}
    monkeypatch.setattr(experiment, "cmd", lambda *args, **kwargs: json.dumps(created))
    owner.create(timeout=30)
    changed_status = copy.deepcopy(created)
    changed_status["metadata"].update(generation=7, resourceVersion="20")
    changed_status["status"] = {"experiment": {"desiredPhase": "Run"}}
    reads = iter([changed_status, None])
    monkeypatch.setattr(experiment, "resource", lambda *args: next(reads))
    deleted = []
    monkeypatch.setattr(experiment, "delete_identity", lambda *args: deleted.append(args))
    owner.cleanup()
    assert len(deleted) == 1 and deleted[0][3]["metadata"]["resourceVersion"] == "20"
    assert owner.expected_spec == created["spec"]


def test_network_spec_change_still_blocks_cleanup_after_status_updates(monkeypatch):
    owner = experiment.OwnedResource({"kind": "NetworkChaos", "metadata": {"name": "checkout-slow-pod", "namespace": "demo"}, "spec": {}}, "this-run")
    created = {"metadata": {"uid": "own-chaos", "generation": 1, "resourceVersion": "10",
                            "annotations": {experiment.OWNER_ANNOTATION: "this-run"}}, "spec": {"action": "delay"}}
    monkeypatch.setattr(experiment, "cmd", lambda *args, **kwargs: json.dumps(created))
    owner.create(timeout=30)
    changed = copy.deepcopy(created)
    changed["metadata"]["generation"] = 7
    changed["spec"]["action"] = "partition"
    monkeypatch.setattr(experiment, "resource", lambda *args: changed)
    monkeypatch.setattr(experiment, "delete_identity", lambda *args: pytest.fail("Чужая перенастройка не удаляется"))
    with pytest.raises(RuntimeError, match="очистка запрещена"):
        owner.cleanup()


def test_rejected_capacity_profile_preserves_json_without_any_mutation(monkeypatch, tmp_path):
    capacity = tmp_path / "capacity.json"
    capacity.write_text('{"passed": false}', encoding="utf-8")
    output = tmp_path / "result.json"
    commands = []
    monkeypatch.setattr(sys, "argv", ["experiment", "--mode", "auto", "--scenario", "spike",
                                     "--spike-capacity", str(capacity), "--output", str(output)])
    monkeypatch.setattr(experiment, "cmd", lambda *args, **kwargs: commands.append(args) or json.dumps({"items": []}))
    monkeypatch.setattr(experiment, "deployment", lambda name, namespace: {"spec": {"replicas": 2 if name == "checkout" else 1}})
    monkeypatch.setattr(experiment, "wait_healthy", lambda *args: None)
    monkeypatch.setattr(experiment, "wait_incident_closed", lambda: {"incident_closed": True})
    with pytest.raises(SystemExit, match="Замеры мощности"):
        experiment.main()
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["passed"] is False and result["cleanup_errors"] == [] and result["resource_intents"] == []
    assert not any(args[0] == "kubectl" and any(verb in args for verb in ("delete", "patch", "scale", "apply", "create")) for args in commands)
