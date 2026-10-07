"""Ошибка инъекции или очистки не теряет отчет и не оставляет собственный процесс без попытки завершения."""
import base64
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
spec = importlib.util.spec_from_file_location("scenarios_script", Path(__file__).resolve().parents[2] / "scripts/verify-scenarios.py")
scenarios = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scenarios)
import experiment


def test_cleanup_error_keeps_failed_json_and_stops_forward(monkeypatch, tmp_path):
    target, stopped = tmp_path / "result.json", []
    monkeypatch.setattr(sys, "argv", ["scenarios", "--output", str(target)])
    def command(*args, **kwargs):
        if args[:3] == ("kubectl", "get", "secret"):
            return json.dumps({"data": {"token": base64.b64encode(b"controlled-not-secret").decode()}})
        if args[:3] == ("kubectl", "get", "pods"):
            return json.dumps({"items": [{"metadata": {"uid": "owned-pod"}}]})
        return json.dumps({"items": []})
    monkeypatch.setattr(scenarios, "cmd", command)
    monkeypatch.setattr(scenarios, "wait_healthy", Mock(side_effect=[None, RuntimeError("Контрольный отказ SLO")]))
    monkeypatch.setattr(scenarios, "wait_incident_closed", lambda: {})
    child = SimpleNamespace(terminate=lambda: stopped.append("terminate"), wait=lambda timeout: stopped.append(timeout))
    monkeypatch.setattr(scenarios.subprocess, "Popen", lambda *args, **kwargs: child)
    def create(scenario, marker, owned):
        owner = SimpleNamespace(cleanup=Mock(side_effect=RuntimeError("Контрольный отказ очистки")))
        owned.append(("podchaos/checkout-kill", owner, False))
        return "own-chaos"
    monkeypatch.setattr(scenarios, "inject", create)

    with pytest.raises(SystemExit):
        scenarios.main()
    report = json.loads(target.read_text(encoding="utf-8"))
    assert report["passed"] is False and report["forward_stopped"] is True
    assert report["cleanup_errors"] == [{"resource": "podchaos/checkout-kill", "error_type": "RuntimeError"}]
    assert report["unfinished_resources"] == ["podchaos/checkout-kill"]
    assert stopped == ["terminate", 5] and report["cases"][0]["passed"] is False
    assert "controlled-not-secret" not in target.read_text(encoding="utf-8")


def test_unknown_create_is_not_repeated_and_intent_precedes_api(monkeypatch):
    owned, calls = [], []
    monkeypatch.chdir(Path(__file__).resolve().parents[2])
    def lost(*args, **kwargs):
        calls.append(args)
        assert len(owned) == 1
        raise RuntimeError("Ответ POST потерян")
    monkeypatch.setattr(experiment, "cmd", lost)
    monkeypatch.setattr(experiment, "resource", lambda *args: None)
    with pytest.raises(RuntimeError, match="потерян"):
        scenarios.inject("kill", "controlled", owned)
    errors = []
    scenarios.cleanup_owned(owned, errors)
    assert len(calls) == 1 and calls[0][:2] == ("kubectl", "create")
    assert owned == [] and errors == []


def test_stalled_forward_is_killed_with_a_second_finite_wait():
    calls, waits, errors = [], [], []
    def wait(timeout):
        waits.append(timeout)
        if len(waits) == 1:
            raise scenarios.subprocess.TimeoutExpired("owned-forward", timeout)
        return 0
    child = SimpleNamespace(terminate=lambda: calls.append("terminate"), kill=lambda: calls.append("kill"), wait=wait)
    assert scenarios.stop_forward(child, errors) is True
    assert calls == ["terminate", "kill"] and waits == [5, 5] and errors == []


def test_terminate_error_still_attempts_to_kill_its_own_process():
    errors, calls = [], []
    child = SimpleNamespace(terminate=Mock(side_effect=RuntimeError("Контрольный отказ terminate")),
                            kill=lambda: calls.append("kill"), wait=lambda timeout: calls.append(timeout))
    assert scenarios.stop_forward(child, errors) is True
    assert calls == ["kill", 5]
    assert errors == [{"resource": "process/port-forward", "error_type": "RuntimeError"}]
