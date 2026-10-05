import json
import logging
import os
import random
import time

from fastapi import FastAPI, Request, Response
from opentelemetry import trace
from opentelemetry.propagate import extract
from opentelemetry.trace import SpanKind, Status, StatusCode
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Histogram, generate_latest
from prometheus_client.openmetrics.exposition import CONTENT_TYPE_LATEST as OPENMETRICS
from prometheus_client.openmetrics.exposition import generate_latest as generate_openmetrics

from app.otel import current_trace_id, setup

SERVICE = "payment"
KNOWN_PATHS = {"/health", "/pay", "/metrics"}

REQUESTS = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)
LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1, 2, 3, 5, 10, 30),
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname.lower(),
            "service": SERVICE,
            "message": record.getMessage(),
        }
        for key in ("trace_id", "path", "status", "latency_ms", "reason"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging() -> logging.Logger:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return logging.getLogger(SERVICE)


log = setup_logging()
setup(SERVICE)
app = FastAPI(title=SERVICE)
TRACED_PATHS = {"/pay"}


def flag(name: str) -> str:
    return os.getenv(name, "").strip()


def fail_rate() -> float:
    raw = flag("FAIL_RATE")
    if raw in {"", "0"}:
        return 0.0
    try:
        return min(1.0, max(0.0, float(raw)))
    except ValueError:
        return 0.0


def extra_latency_seconds() -> float:
    raw = flag("EXTRA_LATENCY_MS")
    if raw in {"", "0"}:
        return 0.0
    try:
        return max(0.0, float(raw)) / 1000.0
    except ValueError:
        return 0.0


def _exemplar(trace_id: str):
    if not trace_id:
        return None
    return {"trace_id": trace_id}


async def _observe(request: Request, call_next, span):
    trace_id = current_trace_id()
    request.state.trace_id = trace_id
    started = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - started
    path = request.url.path if request.url.path in KNOWN_PATHS else "other"
    reason = getattr(request.state, "reason", None)
    if span is not None:
        span.set_attribute("http.route", request.url.path)
        span.set_attribute("http.status_code", response.status_code)
        if reason:
            span.set_attribute("reason", reason)
        if response.status_code >= 500:
            span.set_status(Status(StatusCode.ERROR, reason or "error"))
    if path != "/metrics":
        status = str(response.status_code)
        exemplar = _exemplar(trace_id)
        REQUESTS.labels(request.method, path, status).inc(exemplar=exemplar)
        LATENCY.labels(request.method, path).observe(elapsed, exemplar=exemplar)
        extra = {
            "path": request.url.path,
            "status": response.status_code,
            "latency_ms": round(elapsed * 1000, 2),
        }
        if trace_id:
            extra["trace_id"] = trace_id
        if reason:
            extra["reason"] = reason
        log.info("request", extra=extra)
    if trace_id:
        response.headers["x-trace-id"] = trace_id
    return response


@app.middleware("http")
async def observe(request: Request, call_next):
    if request.url.path not in TRACED_PATHS:
        return await _observe(request, call_next, None)
    with trace.get_tracer(SERVICE).start_as_current_span(
        f"{request.method} {request.url.path}",
        context=extract(request.headers),
        kind=SpanKind.SERVER,
    ) as span:
        return await _observe(request, call_next, span)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/pay")
def pay(request: Request):
    delay = extra_latency_seconds()
    if delay:
        time.sleep(delay)
    trace_id = request.state.trace_id
    if random.random() < fail_rate():
        request.state.reason = "fail_rate"
        return Response(
            content=json.dumps({"status": "error", "reason": "fail_rate", "trace_id": trace_id}),
            media_type="application/json",
            status_code=500,
        )
    request.state.reason = "ok"
    return {"status": "ok", "trace_id": trace_id}


@app.get("/metrics")
def metrics(request: Request):
    accept = request.headers.get("accept", "")
    if "application/openmetrics-text" in accept:
        body = generate_openmetrics(REGISTRY)
        if not isinstance(body, bytes):
            body = b"".join(body)
        return Response(content=body, media_type=OPENMETRICS)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
