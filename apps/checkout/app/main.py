import hashlib
import json
import logging
import os
import random
import time
from contextlib import asynccontextmanager

import httpx
import redis
from fastapi import FastAPI, Request, Response
from opentelemetry import trace
from opentelemetry.propagate import extract, inject
from opentelemetry.trace import SpanKind, Status, StatusCode
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Histogram, generate_latest, start_http_server
from prometheus_client.openmetrics.exposition import CONTENT_TYPE_LATEST as OPENMETRICS
from prometheus_client.openmetrics.exposition import generate_latest as generate_openmetrics

from app.otel import current_trace_id, setup

SERVICE = "checkout"
KNOWN_PATHS = {"/health", "/work", "/metrics"}
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1, 2, 3, 5, 10, 30)

REQUESTS = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)
LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path"],
    buckets=BUCKETS,
)
DEPENDENCY_ERRORS = Counter(
    "dependency_errors_total",
    "Failed calls from checkout to a dependency",
    ["dependency", "reason"],
)
APPLICATION_ERRORS = Counter(
    "application_errors_total",
    "Ошибки внутри приложения checkout",
    ["reason"],
)
DEPENDENCY_LATENCY = Histogram(
    "dependency_duration_seconds",
    "Time spent calling a dependency",
    ["dependency"],
    buckets=BUCKETS,
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


@asynccontextmanager
async def lifespan(app):
    server, _ = start_http_server(int(os.getenv("METRICS_PORT", "8081")))
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()


app = FastAPI(title=SERVICE, lifespan=lifespan)
_redis = None
TRACED_PATHS = {"/work"}


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


def cpu_iters() -> int:
    raw = flag("CPU_ITERS")
    if raw in {"", "0"}:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def burn_cpu() -> None:
    iters = cpu_iters()
    if iters <= 0:
        return
    digest = b"checkout"
    for _ in range(iters):
        digest = hashlib.sha256(digest).digest()


def extra_latency_seconds() -> float:
    raw = flag("EXTRA_LATENCY_MS")
    if raw in {"", "0"}:
        return 0.0
    try:
        return max(0.0, float(raw)) / 1000.0
    except ValueError:
        return 0.0


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        timeout = float(os.getenv("REDIS_SOCKET_TIMEOUT", "3"))
        _redis = redis.Redis.from_url(url, socket_connect_timeout=timeout, socket_timeout=timeout)
    return _redis


def error_body(reason: str, trace_id: str, status_code: int = 500) -> Response:
    return Response(
        content=json.dumps({"status": "error", "reason": reason, "trace_id": trace_id}),
        media_type="application/json",
        status_code=status_code,
    )


def redis_reason(exc: Exception) -> str:
    if isinstance(exc, redis.TimeoutError):
        return "timeout"
    return "error"


def save_cart(trace_id: str) -> int:
    with trace.get_tracer(SERVICE).start_as_current_span("redis", kind=SpanKind.CLIENT) as span:
        span.set_attribute("db.system", "redis")
        client = get_redis()
        seq = int(client.incr("cart:seq"))
        client.set(
            "cart:current",
            json.dumps({"seq": seq, "trace_id": trace_id, "sku": "demo", "qty": 1}),
        )
        return seq


def call_payment(trace_id: str) -> str:
    url = os.getenv("PAYMENT_URL", "http://payment/pay")
    timeout = float(os.getenv("PAYMENT_TIMEOUT", "3"))
    with trace.get_tracer(SERVICE).start_as_current_span("POST /pay", kind=SpanKind.CLIENT) as span:
        headers = {}
        inject(headers)
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, headers=headers)
        span.set_attribute("http.status_code", response.status_code)
        if response.status_code >= 500:
            span.set_status(Status(StatusCode.ERROR, "http"))
            return "http"
        response.raise_for_status()
        return "ok"


def observe_dependency(dependency: str, started: float, reason: str) -> None:
    exemplar = _exemplar(current_trace_id())
    DEPENDENCY_LATENCY.labels(dependency).observe(time.perf_counter() - started, exemplar=exemplar)
    if reason != "ok":
        DEPENDENCY_ERRORS.labels(dependency, reason).inc(exemplar=exemplar)


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


@app.get("/work")
def work(request: Request):
    delay = extra_latency_seconds()
    if delay:
        time.sleep(delay)
    burn_cpu()
    trace_id = request.state.trace_id
    if random.random() < fail_rate():
        APPLICATION_ERRORS.labels("fail_rate").inc(exemplar=_exemplar(trace_id))
        request.state.reason = "fail_rate"
        return error_body("fail_rate", trace_id)

    started = time.perf_counter()
    try:
        cart_seq = save_cart(trace_id)
    except redis.RedisError as exc:
        reason = redis_reason(exc)
        observe_dependency("redis", started, reason)
        request.state.reason = f"redis_{reason}"
        return error_body(f"redis_{reason}", trace_id)
    observe_dependency("redis", started, "ok")

    started = time.perf_counter()
    try:
        payment_reason = call_payment(trace_id)
    except httpx.TimeoutException:
        observe_dependency("payment", started, "timeout")
        request.state.reason = "payment_timeout"
        return error_body("payment_timeout", trace_id)
    except httpx.HTTPError:
        observe_dependency("payment", started, "error")
        request.state.reason = "payment_error"
        return error_body("payment_error", trace_id)
    if payment_reason != "ok":
        observe_dependency("payment", started, payment_reason)
        request.state.reason = f"payment_{payment_reason}"
        return error_body(f"payment_{payment_reason}", trace_id)
    observe_dependency("payment", started, "ok")

    request.state.reason = "ok"
    return {"status": "ok", "cart_seq": cart_seq, "payment": "ok", "trace_id": trace_id}


@app.get("/metrics")
def metrics(request: Request):
    accept = request.headers.get("accept", "")
    if "application/openmetrics-text" in accept:
        body = generate_openmetrics(REGISTRY)
        if not isinstance(body, bytes):
            body = b"".join(body)
        return Response(content=body, media_type=OPENMETRICS)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
