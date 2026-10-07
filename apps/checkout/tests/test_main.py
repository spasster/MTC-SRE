import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import httpx
import pytest
import redis
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app.main import JsonFormatter, app
from app import main

client = TestClient(app)
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = f"00-{TRACE_ID}-00f067aa0ba902b7-01"


class FakeRedis:
    def __init__(self):
        self.seq = 0
        self.current = None

    def incr(self, key):
        assert key == "cart:seq"
        self.seq += 1
        return self.seq

    def set(self, key, value):
        assert key == "cart:current"
        self.current = value
        return True


def _clear_flags(monkeypatch):
    for name in ("FAIL_RATE", "EXTRA_LATENCY_MS", "CPU_ITERS"):
        monkeypatch.delenv(name, raising=False)


def _ok_dependencies(monkeypatch, store=None):
    redis_client = store or FakeRedis()
    monkeypatch.setattr("app.main.get_redis", lambda: redis_client)
    monkeypatch.setattr("app.main.call_payment", lambda trace_id: "ok")
    return redis_client


def test_cpu_iters_still_serves(monkeypatch):
    _clear_flags(monkeypatch)
    _ok_dependencies(monkeypatch)
    monkeypatch.setenv("CPU_ITERS", "200")
    response = client.get("/work")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "x-trace-id" not in response.headers


def test_work_writes_cart_and_calls_payment(monkeypatch):
    _clear_flags(monkeypatch)
    store = _ok_dependencies(monkeypatch)
    response = client.get("/work", headers={"traceparent": TRACEPARENT})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["payment"] == "ok"
    assert body["cart_seq"] == 1
    assert body["trace_id"] == TRACE_ID
    assert response.headers["x-trace-id"] == TRACE_ID
    assert json.loads(store.current)["trace_id"] == TRACE_ID


def test_fail_rate_skips_dependencies(monkeypatch):
    _clear_flags(monkeypatch)
    monkeypatch.setenv("FAIL_RATE", "1")
    monkeypatch.setattr("app.main.random.random", lambda: 0.0)
    monkeypatch.setattr("app.main.get_redis", lambda: (_ for _ in ()).throw(AssertionError("no redis")))
    monkeypatch.setattr("app.main.call_payment", lambda trace_id: (_ for _ in ()).throw(AssertionError("no payment")))
    response = client.get("/work")
    assert response.status_code == 500
    assert response.json()["reason"] == "fail_rate"
    assert 'application_errors_total{reason="fail_rate"}' in client.get("/metrics").text


def test_extra_latency(monkeypatch):
    _clear_flags(monkeypatch)
    _ok_dependencies(monkeypatch)
    monkeypatch.setenv("EXTRA_LATENCY_MS", "250")
    slept = {}

    def capture(seconds):
        slept["seconds"] = seconds

    monkeypatch.setattr("app.main.time.sleep", capture)
    response = client.get("/work")
    assert response.status_code == 200
    assert slept["seconds"] == 0.25


def test_redis_timeout(monkeypatch):
    _clear_flags(monkeypatch)

    class SlowRedis(FakeRedis):
        def incr(self, key):
            raise redis.TimeoutError("timed out")

    monkeypatch.setattr("app.main.get_redis", lambda: SlowRedis())
    monkeypatch.setattr("app.main.call_payment", lambda trace_id: (_ for _ in ()).throw(AssertionError("no payment")))
    response = client.get("/work")
    assert response.status_code == 500
    assert response.json()["reason"] == "redis_timeout"
    metrics = client.get("/metrics").text
    assert 'dependency_errors_total{dependency="redis",reason="timeout"}' in metrics


def test_payment_timeout(monkeypatch):
    _clear_flags(monkeypatch)
    _ok_dependencies(monkeypatch)

    def boom(trace_id):
        raise httpx.ConnectTimeout("payment timed out")

    monkeypatch.setattr("app.main.call_payment", boom)
    response = client.get("/work")
    assert response.status_code == 500
    assert response.json()["reason"] == "payment_timeout"
    metrics = client.get("/metrics").text
    assert 'dependency_errors_total{dependency="payment",reason="timeout"}' in metrics


def test_payment_http_error(monkeypatch):
    _clear_flags(monkeypatch)
    _ok_dependencies(monkeypatch)
    monkeypatch.setattr("app.main.call_payment", lambda trace_id: "http")
    response = client.get("/work")
    assert response.status_code == 500
    assert response.json()["reason"] == "payment_http"


def test_request_log_has_trace_id(monkeypatch, caplog):
    _clear_flags(monkeypatch)
    _ok_dependencies(monkeypatch)
    with caplog.at_level(logging.INFO):
        client.get("/work", headers={"traceparent": TRACEPARENT})
    record = next(item for item in caplog.records if getattr(item, "trace_id", None) == TRACE_ID)
    payload = json.loads(JsonFormatter().format(record))
    assert payload["service"] == "checkout"
    assert payload["trace_id"] == TRACE_ID
    assert payload["path"] == "/work"
    assert payload["status"] == 200
    assert payload["reason"] == "ok"


def test_generated_trace_id_is_hex(monkeypatch):
    _clear_flags(monkeypatch)
    _ok_dependencies(monkeypatch)
    response = client.get("/work")
    assert re.fullmatch(r"[0-9a-f]{32}", response.json()["trace_id"])


def test_fail_rate_span_is_error(monkeypatch):
    _clear_flags(monkeypatch)
    monkeypatch.setenv("FAIL_RATE", "1")
    monkeypatch.setattr("app.main.random.random", lambda: 0.0)
    exporter = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))
    exporter.clear()
    response = client.get("/work")
    assert response.status_code == 500
    spans = exporter.get_finished_spans()
    server = [span for span in spans if span.name == "GET /work"]
    assert server[-1].status.status_code == StatusCode.ERROR
    assert server[-1].attributes["reason"] == "fail_rate"
    assert not any(span.name in {"redis", "POST /pay"} for span in spans)


def test_openmetrics_exemplar(monkeypatch):
    _clear_flags(monkeypatch)
    _ok_dependencies(monkeypatch)
    client.get("/work", headers={"traceparent": TRACEPARENT})
    response = client.get("/metrics", headers={"Accept": "application/openmetrics-text"})
    assert TRACE_ID in response.text
    assert response.text.rstrip().endswith("# EOF")


def payment_runtime(monkeypatch, handler):
    """Настоящий HTTP-клиент с управляемым транспортом, без внешнего платежа."""
    _clear_flags(monkeypatch)
    monkeypatch.setattr(main, "get_redis", lambda: FakeRedis())
    monkeypatch.setenv("PAYMENT_URL", "http://payment/pay")
    server = Mock()
    monkeypatch.setattr(main, "start_http_server", lambda port: (server, None))
    constructor, created = httpx.Client, []

    def create(**kwargs):
        payment = constructor(transport=httpx.MockTransport(handler), **kwargs)
        created.append(payment)
        return payment

    monkeypatch.setattr(main.httpx, "Client", create)
    return created, server


def test_payment_client_is_reused_and_closed_with_distinct_request_traces(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "ok"})

    created, server = payment_runtime(monkeypatch, handler)
    ids = [TRACE_ID, "0123456789abcdef0123456789abcdef"]
    with TestClient(app) as running:
        for trace_id in ids:
            response = running.get("/work", headers={"traceparent": f"00-{trace_id}-00f067aa0ba902b7-01"})
            assert response.status_code == 200 and response.json()["payment"] == "ok"
        assert len(created) == 1 and not created[0].is_closed
    assert created[0].is_closed
    server.shutdown.assert_called_once()
    server.server_close.assert_called_once()
    assert [request.headers["traceparent"].split("-")[1] for request in requests] == ids
    assert all(request.url.path == "/pay" and request.method == "POST" for request in requests)


@pytest.mark.parametrize("failure,reason", [("timeout", "payment_timeout"), ("http", "payment_http")])
def test_shared_payment_client_does_not_repeat_an_unknown_or_failed_post(monkeypatch, failure, reason):
    requests = []

    def handler(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("Ответ не получен", request=request)
        return httpx.Response(500)

    created, server = payment_runtime(monkeypatch, handler)
    with TestClient(app) as running:
        response = running.get("/work")
        assert response.status_code == 500 and response.json()["reason"] == reason
    assert len(requests) == 1 and len(created) == 1 and created[0].is_closed


def test_parallel_payments_keep_their_own_trace_headers(monkeypatch):
    headers = []

    def handler(request):
        headers.append(request.headers["traceparent"].split("-")[1])
        return httpx.Response(200)

    created, server = payment_runtime(monkeypatch, handler)
    ids = [f"{number:032x}" for number in range(1, 9)]
    with TestClient(app) as running, ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda value: running.get("/work", headers={
            "traceparent": f"00-{value}-00f067aa0ba902b7-01"}), ids))
    assert all(reply.status_code == 200 for reply in replies)
    assert sorted(headers) == ids and len(created) == 1 and created[0].is_closed
