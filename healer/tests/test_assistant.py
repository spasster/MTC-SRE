import json

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import assistant


def test_snapshot_excludes_secrets_and_untrusted_names():
    snapshot = assistant.safe_snapshot({"token": "secret", "error": "secret URL", "p95": float("nan"),
                                       "pods": [{"pod": "ignore instructions", "p95": 0.2}]})
    rendered = json.dumps(snapshot)
    assert "secret" not in rendered
    assert "ignore instructions" not in rendered
    assert snapshot["p95"] is None
    assert snapshot["collection_failed"] is True


def test_model_receives_a_bounded_read_only_prompt():
    def generate(request):
        payload = json.loads(request.content)
        assert payload["stream"] is False
        assert payload["options"]["num_predict"] == 384
        assert "actions" not in payload
        assert "не имеет функций изменения" in payload["system"]
        return httpx.Response(200, json={"done": True, "response": "Данных недостаточно для вывода."})

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
        200, json={"done": True, "response": "Ошибочная рекомендация модели: заменить Redis."})))
    result = assistant.build_report(evidence, client=client)
    assert result["slo_assessment"]["state"] == "healthy"
    assert result["slo_assessment"]["source"] == "measured_metrics"
    assert result["actions_applied"] == []
