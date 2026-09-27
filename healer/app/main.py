import json
import logging
import os
import threading
import time

from fastapi import FastAPI

from app.k8s import K8sActuator

SERVICE = "healer"
WHITELIST = {"CheckoutSlow", "CheckoutHighErrorRate"}
ALLOWED_DEPLOYMENTS = {"checkout"}
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "300"))
TARGET_NAMESPACE = os.getenv("TARGET_NAMESPACE", "demo")

_last_handled = {}
_lock = threading.Lock()
actuator = None


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname.lower(),
            "service": SERVICE,
            "message": record.getMessage(),
        }
        for key in ("alertname", "action", "result"):
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


def get_actuator():
    global actuator
    if actuator is None:
        actuator = K8sActuator()
    return actuator


def recently_handled(alertname: str) -> bool:
    with _lock:
        last = _last_handled.get(alertname)
        return last is not None and time.monotonic() - last < COOLDOWN_SECONDS


def mark_handled(alertname: str) -> None:
    with _lock:
        _last_handled[alertname] = time.monotonic()


def reset_state() -> None:
    global actuator
    with _lock:
        _last_handled.clear()
    actuator = None


def apply_action(action: str, deployment: str) -> str:
    actor = get_actuator()
    if action == "scale":
        return actor.scale(TARGET_NAMESPACE, deployment, 5)
    if action == "restart":
        return actor.restart(TARGET_NAMESPACE, deployment)
    if action == "rollback":
        return actor.rollback(TARGET_NAMESPACE, deployment)
    return "ignored: unknown action"


def handle_alert(alert: dict) -> dict:
    labels = alert.get("labels") or {}
    alertname = labels.get("alertname", "")
    action = labels.get("action", "")
    deployment = labels.get("service", "checkout")
    if alert.get("status") != "firing":
        return {"alertname": alertname, "result": "ignored: not firing"}
    if alertname not in WHITELIST:
        return {"alertname": alertname, "result": "ignored: not in whitelist"}
    if deployment not in ALLOWED_DEPLOYMENTS:
        return {"alertname": alertname, "result": "ignored: deployment not allowed"}
    if action not in {"scale", "restart", "rollback"}:
        return {"alertname": alertname, "result": "ignored: unknown action"}
    if recently_handled(alertname):
        return {"alertname": alertname, "result": "ignored: cooldown"}
    result = apply_action(action, deployment)
    mark_handled(alertname)
    log.info("handled", extra={"alertname": alertname, "action": action, "result": result})
    return {"alertname": alertname, "action": action, "result": result}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/webhook")
def webhook(payload: dict):
    alerts = payload.get("alerts") or []
    results = [handle_alert(alert) for alert in alerts]
    return {"results": results}
