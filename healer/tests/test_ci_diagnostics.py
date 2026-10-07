"""Причина отказа допуска сохраняется без закрытого содержимого ConfigMap."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("ci_diagnostics", Path(__file__).resolve().parents[2] / "scripts/collect-ci-diagnostics.py")
diagnostics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostics)


def test_diagnostics_keeps_stuck_timer_without_private_pending_or_events(monkeypatch):
    private = "controlled-private-value"
    state = {"_actions": 0, "_broke_at": 100, "_resolved_pending": False,
             "_pending": {"approval_id": private, "detail": private}, "_closed": [], "_recovery_targets": []}
    document = {"state": state, "events": [{"operator_id": private}], "operations": [{"result": private}]}
    obj = {"data": {"state.json": json.dumps(document)}, "metadata": {"annotations": {"mtc-sre/state-initialized": "true"}}}
    monkeypatch.setattr(diagnostics.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(obj)))
    result = diagnostics.healer_state()
    assert result["state"]["_broke_at"] == 100 and result["state"]["_resolved_pending"] is False
    assert result["state"]["_pending_present"] and result["operations_count"] == result["events_count"] == 1
    assert private not in json.dumps(result) and result["initialized"]


@pytest.mark.parametrize("value", [True, float("nan"), "controlled-private-value"])
def test_invalid_numeric_state_cannot_leak_or_break_json(monkeypatch, value):
    obj = {"data": {"state.json": json.dumps({"state": {"_broke_at": value}})}}
    monkeypatch.setattr(diagnostics.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(obj)))
    result = diagnostics.healer_state()
    assert result["state"]["_broke_at"] is None
    assert "controlled-private-value" not in json.dumps(result, allow_nan=False)


def test_diagnostics_read_failure_is_explicit_and_does_not_echo_stderr(monkeypatch):
    monkeypatch.setattr(diagnostics.subprocess, "run", lambda *args, **kwargs:
        SimpleNamespace(returncode=1, stdout="", stderr="controlled-private-value"))
    result = diagnostics.healer_state()
    assert result == {"available": False, "error_type": "KubernetesReadError"}
