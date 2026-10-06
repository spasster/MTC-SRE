import json
import math
import os
import re
from pathlib import Path

import httpx
import yaml
from fastapi import Depends, FastAPI, HTTPException

from app.diagnose import decide
from app.evidence import gather
from app.main import authorize_webhook

app = FastAPI(title="SRE-ассистент")
NUMERIC_FIELDS = (
    "replicas", "request_rate", "p95", "revision_age_seconds", "error_ratio",
    "application_error_ratio", "redis_error_ratio", "payment_error_ratio",
    "payment_timeout_ratio", "synthetic_success", "synthetic_age_seconds",
)


def safe_snapshot(evidence):
    snapshot = {"collection_failed": bool(evidence.get("error"))}
    for key in NUMERIC_FIELDS:
        value = evidence.get(key)
        snapshot[key] = value if isinstance(value, (int, float)) and math.isfinite(value) else None
    for key in ("redis_ready", "payment_ready"):
        snapshot[key] = evidence.get(key) is True
    snapshot["pods"] = []
    for pod in (evidence.get("pods") or [])[:10]:
        name = str(pod.get("pod", ""))
        item = {"pod": name if re.fullmatch(r"[a-z0-9.-]{1,253}", name) else "неизвестно"}
        for key in ("p95", "p95_recent", "throttle"):
            value = pod.get(key)
            item[key] = value if isinstance(value, (int, float)) and math.isfinite(value) else None
        snapshot["pods"].append(item)
    return snapshot


def build_report(evidence, client=None):
    snapshot = safe_snapshot(evidence)
    source = Path(__file__).resolve().parents[1]
    rules = yaml.safe_load((source / "rules.yaml").read_text(encoding="utf-8"))
    decision = decide(evidence, rules).as_dict()
    payload = {
        "model": os.getenv("OLLAMA_MODEL", "qwen2.5:1.5b"),
        "system": (source / "prompts/incident.md").read_text(encoding="utf-8"),
        "prompt": json.dumps({"метрики": snapshot, "решение_правил": decision}, ensure_ascii=False),
        "stream": False,
        "options": {"num_predict": 384, "num_ctx": 2048, "num_thread": 2, "temperature": 0.1},
        "keep_alive": "5m",
    }
    own_client = client is None
    http = client or httpx.Client(timeout=120)
    try:
        response = http.post(os.getenv("OLLAMA_URL", "http://ollama.assist.svc:11434").rstrip("/") + "/api/generate",
                             json=payload)
        response.raise_for_status()
        body = response.json()
        answer = body.get("response") if isinstance(body, dict) else None
        if not isinstance(answer, str) or not answer.strip() or body.get("done") is not True:
            raise ValueError("Неполный ответ модели")
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=503, detail="Локальная модель недоступна или ответ не завершён") from None
    finally:
        if own_client:
            http.close()
    return {"source": "local_llm", "model": payload["model"], "snapshot": snapshot,
            "rule_decision": decision, "report": answer[:16000], "actions_applied": [],
            "notice": "Рекомендации модели требуют проверки. Ассистент не меняет кластер."}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/report", dependencies=[Depends(authorize_webhook)])
def report():
    return build_report(gather(os.getenv("TARGET_NAMESPACE", "demo")))
