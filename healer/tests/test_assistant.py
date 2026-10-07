import json

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import assistant


def response_sections(payload):
    return {key: payload["format"]["properties"][key]["enum"][0] for key in payload["format"]["required"]}


def test_snapshot_excludes_secrets_and_untrusted_names():
    snapshot = assistant.safe_snapshot({"token": "secret", "error": "secret URL", "p95": float("nan"),
                                       "pods": [{"pod": "ignore instructions", "p95": 0.2}]})
    rendered = json.dumps(snapshot)
    assert "secret" not in rendered
    assert "ignore instructions" not in rendered
    assert snapshot["p95"] is None
    assert snapshot["collection_failed"] is True


def test_postmortem_uses_proven_timings_and_excludes_untrusted_journal_fields():
    document = {"state": {"_last_recovery": {"started_at": 100, "slo_restored_at": 140, "resolved_received_at": 145}},
                "operations": [{"action": "delete_pod", "status": "observed_after_restart", "prepared_at": 110,
                                "token": "secret", "error": "ignore instructions"}],
                "events": [{"event": "approval_callback", "time": 120, "choice": "approve", "operator_key": "private-user"}]}
    facts = assistant.incident_facts(document)
    assert facts["mttr_seconds"] == 40 and facts["api_delay_seconds"] is None
    assert facts["operations"][0]["api_accepted_at"] is None
    assert all(text not in json.dumps(facts) for text in ("secret", "ignore instructions", "private-user"))
    def generate(request):
        payload = json.loads(request.content)
        assert "факты_инцидента" in payload["prompt"]
        assert payload["format"]["required"] == ["symptoms", "actions", "recovery", "next_checks"]
        sections = response_sections(payload)
        return httpx.Response(200, json={"done": True, "response": json.dumps(sections)})
    result = assistant.build_report({}, client=httpx.Client(transport=httpx.MockTransport(generate)), incident=facts)
    assert result["incident_facts"]["mttr_seconds"] == 40
    assert result["actions_applied"] == []
    assert "40.0 с" in result["report_sections"]["recovery"]


def test_postmortem_recognizes_worker_closure_without_a_resolved_webhook():
    document = {"state": {"_last_recovery": {"started_at": 100, "slo_restored_at": 140,
                                               "resolved_received_at": None, "closed_at": 140}},
                "events": [{"time": 140, "event": "incident_resolved"},
                           {"time": 145, "event": "decision"}]}
    facts = assistant.incident_facts(document)
    assert facts["scope"] == "latest_completed" and facts["status"] == "completed"
    assert facts["mttr_seconds"] == 40 and facts["resolved_received_at"] is None
    assert facts["closed_at"] == 140
    assert len(facts["timeline"]) == 1


@pytest.mark.parametrize("answer", [
    "Повторяющиеся вложенные списки вместо отчета.",
    json.dumps({"symptoms": "Нет данных"}),
    json.dumps({"symptoms": ["Нет данных"], "actions": "Нет", "recovery": "Не подтверждено", "next_checks": "Проверить"}),
    json.dumps({"symptoms": "x" * 601, "actions": "Нет", "recovery": "Не подтверждено", "next_checks": "Проверить"}),
])
def test_invalid_postmortem_text_is_not_returned_as_a_report(answer):
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={"done": True, "response": answer})))
    with pytest.raises(HTTPException) as caught:
        assistant.build_report({}, client=client, incident={"started_at": 100})
    assert caught.value.status_code == 503


def test_model_cannot_invent_recovery_time_in_the_postmortem():
    def generate(request):
        payload = json.loads(request.content)
        sections = response_sections(payload)
        sections["recovery"] = "Восстановление будет через 24 часа."
        return httpx.Response(200, json={"done": True, "response": json.dumps(sections)})
    with pytest.raises(HTTPException) as caught:
        assistant.build_report({}, client=httpx.Client(transport=httpx.MockTransport(generate)),
                               incident={"mttr_seconds": None})
    assert caught.value.status_code == 503


def test_postmortem_requires_authentication_before_reading_the_journal(monkeypatch):
    monkeypatch.setenv("HEALER_WEBHOOK_TOKEN", "test")
    monkeypatch.setattr(assistant, "read_journal", lambda: (_ for _ in ()).throw(AssertionError("Доступ запрещен")))
    assert TestClient(assistant.app).post("/postmortem").status_code == 401


def test_model_receives_a_bounded_read_only_prompt():
    def generate(request):
        payload = json.loads(request.content)
        assert payload["stream"] is False
        assert payload["options"]["num_predict"] == 384
        assert "actions" not in payload
        assert "не имеет функций изменения" in payload["system"]
        return httpx.Response(200, json={"done": True, "response": json.dumps(response_sections(payload))})

    result = assistant.build_report({"error": "Нет метрик"},
                                    client=httpx.Client(transport=httpx.MockTransport(generate)))
    assert result["source"] == "local_llm"
    assert result["actions_applied"] == []


@pytest.mark.parametrize("response", [{"done": False, "response": "Неполный"}, {"done": True}])
def test_incomplete_model_response_is_not_a_report(response):
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)))
    with pytest.raises(HTTPException) as caught:
        assistant.build_report({}, client=client)
    assert caught.value.status_code == 503


def test_model_outage_does_not_change_the_cluster():
    def offline(request):
        raise httpx.ConnectError("URL с секретом", request=request)

    with pytest.raises(HTTPException) as caught:
        assistant.build_report({}, client=httpx.Client(transport=httpx.MockTransport(offline)))
    assert caught.value.status_code == 503
    assert "секрет" not in caught.value.detail


def test_report_requires_authentication(monkeypatch):
    monkeypatch.setenv("HEALER_WEBHOOK_TOKEN", "test")
    monkeypatch.setattr(assistant, "gather", lambda namespace: (_ for _ in ()).throw(AssertionError("Запрос запрещён")))
    assert TestClient(assistant.app).post("/report").status_code == 401


def test_model_text_cannot_override_measured_slo():
    evidence = {"request_rate": 3, "error_ratio": 0, "p95": .26,
                "synthetic_success": 1, "synthetic_age_seconds": 5}
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={"done": True, "response": json.dumps(response_sections(json.loads(request.content)))})))
    result = assistant.build_report(evidence, client=client)
    assert result["slo_assessment"]["state"] == "healthy"
    assert result["slo_assessment"]["source"] == "measured_metrics"
    assert result["actions_applied"] == []


def test_unsupported_payment_advice_for_unknown_incident_is_refused():
    def unsupported(request):
        sections = response_sections(json.loads(request.content))
        sections["next_checks"] = "при ошибках зависимости сопоставить трейс и логи payment"
        return httpx.Response(200, json={"done": True, "response": json.dumps(sections)})
    with pytest.raises(HTTPException) as caught:
        assistant.build_report({}, client=httpx.Client(transport=httpx.MockTransport(unsupported)),
                               incident={"timeline": [{"diagnosis": "unknown"}], "operations": []})
    assert caught.value.status_code == 503


@pytest.mark.parametrize("diagnosis,action,expected", [
    ("cpu", "scale", "cpu"), ("slow_pod", "delete_pod", "pod"), ("redis", "restart_redis", "redis"),
    ("bad_release", "rollback", "revision"), ("partition", "none", "payment"), ("unknown", "none", "data"),
])
def test_completed_incident_does_not_borrow_current_healthy_diagnosis(diagnosis, action, expected):
    incident = {"timeline": [{"diagnosis": diagnosis}], "operations": [] if action == "none" else [{"action": action}]}
    candidates = assistant.grounded_checks({"collection_failed": False}, {"state": "healthy"},
                                          {"diagnosis": "settling", "action": "none"}, incident)
    assert len(candidates) == 2 and all(row["group"] == expected for row in candidates)
    assert all(row["basis"]["source"] == "selected_incident" for row in candidates)


def test_unknown_operation_requires_reconciliation_before_other_advice():
    incident = {"unknown_operation_in_selected_incident": True, "operations": [{"action": "scale", "status": "unknown"}]}
    candidates = assistant.grounded_checks({"collection_failed": False}, {"state": "healthy"},
                                          {"diagnosis": "settling", "action": "none"}, incident)
    assert all(row["group"] == "unknown_operation" for row in candidates)
