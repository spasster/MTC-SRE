"""Новый опыт допускается только после закрытого инцидента."""

import json
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
    monkeypatch.setattr(experiment, "cmd", lambda *args: json.dumps({"data": {"state.json": json.dumps({"state": next(states)})}}))
    monkeypatch.setattr(experiment.time, "sleep", lambda duration: None)
    assert experiment.wait_incident_closed()["incident_closed"]
    assert not experiment.closed_incident({})


def test_guard_timeout_prevents_fault_injection(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(sys, "argv", ["experiment", "--mode", "baseline", "--output", str(tmp_path / "result.json")])
    monkeypatch.setattr(experiment, "cmd", lambda *args: commands.append(args) or json.dumps({"items": []}))
    monkeypatch.setattr(experiment, "deployment", lambda name, namespace: {"spec": {"replicas": 2 if name == "checkout" else 1}})
    monkeypatch.setattr(experiment, "wait_healthy", lambda *args: None)
    monkeypatch.setattr(experiment, "wait_incident_closed", lambda: (_ for _ in ()).throw(RuntimeError("Предыдущий инцидент не закрыт")))
    with pytest.raises(SystemExit, match="не закрыт"):
        experiment.main()
    assert not any(args[:3] == ("sh", "chaos/inject.sh", "slow-pod") for args in commands)
    assert not json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))["passed"]
