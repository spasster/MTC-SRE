import json
import logging
import os
import time
import uuid

import redis
from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

SERVICE = "checkout"
KNOWN_PATHS = {"/health", "/work", "/metrics"}

REQUESTS = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)
LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1, 2, 3, 5),
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname.lower(),
            "service": SERVICE,
            "message": record.getMessage(),
        }
        for key in ("trace_id", "path", "status", "latency_ms"):
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
    return logging.getLogger(SERVICE)


log = setup_logging()
app = FastAPI(title=SERVICE)
_redis = None


def flag(name: str) -> str:
    return os.getenv(name, "").strip()


def flag_on(name: str) -> bool:
    return flag(name).lower() in {"1", "true", "yes"}


def delay_seconds() -> float:
    raw = flag("DELAY")
    if raw in {"", "0"}:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        timeout = float(os.getenv("REDIS_SOCKET_TIMEOUT", "3"))
        _redis = redis.Redis.from_url(url, socket_connect_timeout=timeout, socket_timeout=timeout)
    return _redis


@app.middleware("http")
async def observe(request: Request, call_next):
    trace_id = request.headers.get("x-trace-id") or str(uuid.uuid4())
    request.state.trace_id = trace_id
    started = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - started
    path = request.url.path if request.url.path in KNOWN_PATHS else "other"
    if path != "/metrics":
        status = str(response.status_code)
        REQUESTS.labels(request.method, path, status).inc()
        LATENCY.labels(request.method, path).observe(elapsed)
        log.info(
            "request",
            extra={
                "trace_id": trace_id,
                "path": request.url.path,
                "status": response.status_code,
                "latency_ms": round(elapsed * 1000, 2),
            },
        )
    response.headers["x-trace-id"] = trace_id
    return response


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/work")
def work(request: Request):
    delay = delay_seconds()
    if delay:
        time.sleep(delay)
    trace_id = request.state.trace_id
    if flag_on("ERROR"):
        return Response(
            content=json.dumps({"status": "error", "trace_id": trace_id}),
            media_type="application/json",
            status_code=500,
        )
    if flag_on("DEGRADED_MODE"):
        return {"status": "degraded", "redis": False, "trace_id": trace_id}
    try:
        get_redis().ping()
    except redis.RedisError:
        return Response(
            content=json.dumps({"status": "error", "redis": False, "trace_id": trace_id}),
            media_type="application/json",
            status_code=500,
        )
    return {"status": "ok", "redis": True, "trace_id": trace_id}


@app.get("/metrics")
def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
