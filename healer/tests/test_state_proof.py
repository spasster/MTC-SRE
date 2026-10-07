"""Причины отказа проверочного опыта сохраняются без закрытых данных."""

import base64
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import experiment

script = Path(__file__).resolve().parents[2] / "scripts/verify-healer-state.py"
sys.path.insert(0, str(script.parent))
spec = importlib.util.spec_from_file_location("state_proof", script)
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


def test_webhook_outcomes_do_not_save_private_details():
    result = proof.webhook_outcome({"results": [{"action": "none", "mode": "escalate", "diagnosis": "unknown",
        "result": "failed: закрытые данные", "detail": "закрытые данные", "operator_id": "закрытые данные", "approval_id": "закрытые данные"}]})
    assert result == [{"action": "none", "mode": "escalate", "diagnosis": "unknown", "result_class": "failed"}]
    assert "закрытые данные" not in json.dumps(result, ensure_ascii=False)


def test_evidence_diagnostic_uses_an_explicit_field_list(monkeypatch):
    monkeypatch.setattr(proof, "cmd", lambda *args, **kwargs: json.dumps({"telemetry_complete": False,
        "error": "закрытые данные", "private": "закрытые данные", "request_rate": "закрытые данные", "pods": []}))
    result = proof.diagnostic_evidence()
    assert result["error_present"] and result["error_reason"] == "Внешняя или нераспознанная ошибка чтения"
    assert "закрытые данные" not in json.dumps(result, ensure_ascii=False)


def test_unclosed_incident_does_not_inject_or_clean_a_foreign_fault(monkeypatch, tmp_path):
    output = tmp_path / "state.json"
    monkeypatch.setattr(sys, "argv", ["state-proof", "--output", str(output)])
    calls = []
    def read(*args, **kwargs):
        calls.append(args)
        if "secret" in args:
            return json.dumps({"data": {"token": base64.b64encode(b"controlled-test-value").decode()}})
        return json.dumps({"items": []})
    monkeypatch.setattr(proof, "cmd", read)
    monkeypatch.setattr(proof, "deployment", lambda *args: {"spec": {"replicas": 1}})
    monkeypatch.setattr(proof, "wait_healthy", lambda *args, **kwargs: None)
    monkeypatch.setattr(proof, "wait_incident_closed", lambda: (_ for _ in ()).throw(RuntimeError("Инцидент не закрыт")))
    with pytest.raises(SystemExit, match="не закрыт"):
        proof.main()
    assert not any("chaos/inject.sh" in args or "scale" in args for args in calls)
    assert json.loads(output.read_text(encoding="utf-8"))["passed"] is False


def test_unknown_injection_does_not_clear_a_foreign_chaos_object(monkeypatch, tmp_path):
    output = tmp_path / "state.json"
    monkeypatch.setattr(sys, "argv", ["state-proof", "--output", str(output)])
    calls, creations, deletions = [], [], []
    original = {"metadata": {"uid": "healer-original", "generation": 1, "resourceVersion": "10", "annotations": {}},
                "spec": {"replicas": 1}}
    foreign = {"metadata": {"name": "checkout-delay", "namespace": "demo", "uid": "foreign-chaos", "generation": 1,
                             "resourceVersion": "20", "annotations": {experiment.OWNER_ANNOTATION: "other-run"}}}
    def read(*args, **kwargs):
        calls.append(args)
        if "secret" in args:
            return json.dumps({"data": {"token": base64.b64encode(b"controlled-test-value").decode()}})
        if "pods" in args:
            return json.dumps({"items": [{"metadata": {"name": "checkout-owned", "uid": "checkout-uid"},
                                           "status": {"containerStatuses": [{"name": "checkout", "ready": True}]}}]})
        if "--dry-run=client" in args:
            return json.dumps({"kind": "NetworkChaos", "metadata": {"name": "checkout-delay", "namespace": "demo"},
                               "spec": {"selector": {"pods": {"demo": ["before-selection"]}}}})
        return json.dumps({"items": []})
    monkeypatch.setattr(proof, "cmd", read)
    monkeypatch.setattr(proof, "deployment", lambda *args: original)
    monkeypatch.setattr(proof, "wait_healthy", lambda *args, **kwargs: None)
    monkeypatch.setattr(proof, "wait_incident_closed", lambda *args, **kwargs: {"incident_closed": True})
    monkeypatch.setattr(proof, "document", lambda: {"operations": []})
    monkeypatch.setattr(proof, "diagnostic_evidence", lambda: {"telemetry_complete": True})
    monkeypatch.setattr(proof.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(
        terminate=lambda: None, wait=lambda **kwargs: 0, kill=lambda: None))
    def unknown(self, timeout):
        creations.append(self.manifest)
        raise RuntimeError("Результат инъекции неизвестен")
    monkeypatch.setattr(proof.OwnedResource, "create", unknown)
    monkeypatch.setattr(experiment, "resource", lambda *args: foreign)
    monkeypatch.setattr(experiment, "delete_identity", lambda *args: deletions.append(args))
    with pytest.raises(SystemExit, match="неизвестен"):
        proof.main()
    assert len(creations) == 1 and deletions == []
    assert foreign["metadata"]["uid"] == "foreign-chaos"
    assert not any("chaos/inject.sh" in args or "scale" in args or "delete" in args or "patch" in args for args in calls)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert not report["passed"] and report["cleanup_errors"][0]["operation"] == "networkchaos/checkout-delay"
    assert report["resource_intents"][0]["uid_confirmed"] is None
