"""Причины отказа проверочного опыта сохраняются без закрытых данных."""

import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest

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
