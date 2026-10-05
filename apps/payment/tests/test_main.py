import json
import logging
import re

from fastapi.testclient import TestClient

from app.main import JsonFormatter, app

client = TestClient(app)
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = f"00-{TRACE_ID}-00f067aa0ba902b7-01"


def _clear_flags(monkeypatch):
    for name in ("FAIL_RATE", "EXTRA_LATENCY_MS"):
        monkeypatch.delenv(name, raising=False)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "x-trace-id" not in response.headers


def test_pay_ok(monkeypatch):
    _clear_flags(monkeypatch)
    response = client.post("/pay", headers={"traceparent": TRACEPARENT})
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "trace_id": TRACE_ID}
    assert response.headers["x-trace-id"] == TRACE_ID


def test_fail_rate(monkeypatch):
    _clear_flags(monkeypatch)
    monkeypatch.setenv("FAIL_RATE", "1")
    monkeypatch.setattr("app.main.random.random", lambda: 0.0)
    response = client.post("/pay")
    assert response.status_code == 500
    assert response.json()["reason"] == "fail_rate"


def test_extra_latency(monkeypatch):
    _clear_flags(monkeypatch)
    monkeypatch.setenv("EXTRA_LATENCY_MS", "250")
    slept = {}

    def capture(seconds):
        slept["seconds"] = seconds

    monkeypatch.setattr("app.main.time.sleep", capture)
    response = client.post("/pay")
    assert response.status_code == 200
    assert slept["seconds"] == 0.25


def test_request_log_has_trace_id(monkeypatch, caplog):
    _clear_flags(monkeypatch)
    with caplog.at_level(logging.INFO):
        client.post("/pay", headers={"traceparent": TRACEPARENT})
    record = next(item for item in caplog.records if getattr(item, "trace_id", None) == TRACE_ID)
    payload = json.loads(JsonFormatter().format(record))
    assert payload["service"] == "payment"
    assert payload["trace_id"] == TRACE_ID
    assert payload["path"] == "/pay"
    assert payload["status"] == 200
    assert payload["reason"] == "ok"


def test_generated_trace_id_is_hex(monkeypatch):
    _clear_flags(monkeypatch)
    response = client.post("/pay")
    assert re.fullmatch(r"[0-9a-f]{32}", response.json()["trace_id"])
