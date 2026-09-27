import json
import logging

from fastapi.testclient import TestClient

from app.main import JsonFormatter, app

client = TestClient(app)


class FakeRedis:
    def ping(self):
        return True


class BoomRedis:
    def ping(self):
        from redis.exceptions import ConnectionError

        raise ConnectionError("redis down")


def _clear_flags(monkeypatch):
    for name in ("ERROR", "DELAY", "DEGRADED_MODE"):
        monkeypatch.delenv(name, raising=False)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-trace-id"]


def test_work_ok(monkeypatch):
    _clear_flags(monkeypatch)
    monkeypatch.setattr("app.main.get_redis", lambda: FakeRedis())
    response = client.get("/work", headers={"x-trace-id": "trace-1"})
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["redis"] is True
    assert response.json()["trace_id"] == "trace-1"


def test_error_flag(monkeypatch):
    _clear_flags(monkeypatch)
    monkeypatch.setenv("ERROR", "1")
    called = {"redis": False}

    def fail_if_called():
        called["redis"] = True
        raise AssertionError("redis should not be called")

    monkeypatch.setattr("app.main.get_redis", fail_if_called)
    response = client.get("/work")
    assert response.status_code == 500
    assert called["redis"] is False
    metrics = client.get("/metrics").text
    assert 'http_requests_total' in metrics
    assert 'status="500"' in metrics


def test_degraded_skips_redis(monkeypatch):
    _clear_flags(monkeypatch)
    monkeypatch.setenv("DEGRADED_MODE", "1")
    monkeypatch.setattr("app.main.get_redis", lambda: (_ for _ in ()).throw(AssertionError("no redis")))
    response = client.get("/work")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["redis"] is False


def test_redis_down(monkeypatch):
    _clear_flags(monkeypatch)
    monkeypatch.setattr("app.main.get_redis", lambda: BoomRedis())
    response = client.get("/work")
    assert response.status_code == 500
    assert response.json()["redis"] is False


def test_request_log_has_trace_id(monkeypatch, caplog):
    _clear_flags(monkeypatch)
    monkeypatch.setattr("app.main.get_redis", lambda: FakeRedis())
    with caplog.at_level(logging.INFO):
        client.get("/work", headers={"x-trace-id": "trace-log"})
    record = next(item for item in caplog.records if getattr(item, "trace_id", None) == "trace-log")
    payload = json.loads(JsonFormatter().format(record))
    assert payload["service"] == "checkout"
    assert payload["trace_id"] == "trace-log"
    assert payload["path"] == "/work"
    assert payload["status"] == 200
