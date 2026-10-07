"""Новый опыт допускается только после закрытого инцидента."""

import json
import ast
import sys
from pathlib import Path

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
    def fail_cleanup(*args, **kwargs):
        if args[:3] == ("kubectl", "delete", "networkchaos"):
            raise RuntimeError("Очистка не подтверждена")
        return json.dumps({"items": []})
    monkeypatch.setattr(experiment, "cmd", fail_cleanup)
    monkeypatch.setattr(experiment, "deployment", lambda name, namespace: {"spec": {"replicas": 2 if name == "checkout" else 1}})
    monkeypatch.setattr(experiment, "wait_healthy", lambda *args: None)
    monkeypatch.setattr(experiment, "wait_incident_closed", lambda: (_ for _ in ()).throw(RuntimeError("Предыдущий инцидент не закрыт")))
    with pytest.raises(SystemExit):
        experiment.main()
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert not result["passed"] and result["cleanup_errors"]
    assert "не закрыт" in result["error"]


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
