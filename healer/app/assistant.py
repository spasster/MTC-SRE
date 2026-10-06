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
from app.main import authorize_webhook, _healthy
from app.state import KubernetesStore, StateUnavailable

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
        snapshot[key] = value if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) else None
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


def build_report(evidence, client=None, incident=None):
    snapshot = safe_snapshot(evidence)
    source = Path(__file__).resolve().parents[1]
    rules = yaml.safe_load((source / "rules.yaml").read_text(encoding="utf-8"))
    decision = decide(evidence, rules).as_dict()
    complete = (
        not snapshot["collection_failed"]
        and all(snapshot[key] is not None for key in ("p95", "error_ratio", "request_rate", "synthetic_success", "synthetic_age_seconds"))
        and snapshot["request_rate"] > 0 and snapshot["p95"] >= 0
        and 0 <= snapshot["error_ratio"] <= 1 and 0 <= snapshot["synthetic_age_seconds"] <= 45
    )
    assessment = {"state": "healthy" if complete and _healthy(evidence, rules) else ("violated" if complete else "unknown"),
                  "p95_limit_seconds": rules["slow_seconds"], "error_ratio_limit": rules["error_ratio"],
                  "source": "measured_metrics"}
    payload = {
        "model": os.getenv("OLLAMA_MODEL", "qwen2.5:1.5b"),
        "system": (source / "prompts" / ("postmortem.md" if incident is not None else "incident.md")).read_text(encoding="utf-8"),
        "prompt": json.dumps({"оценка_SLO_сейчас": assessment, "метрики_сейчас": snapshot, "решение_правил_сейчас": decision,
                              **({"факты_инцидента": incident} if incident is not None else {})}, ensure_ascii=False),
        "stream": False,
        "options": {"num_predict": 384, "num_ctx": 4096 if incident is not None else 2048, "num_thread": 2, "temperature": 0.1},
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
    return {"source": "local_llm", "model": payload["model"], "snapshot": snapshot, "slo_assessment": assessment,
            **({"incident_facts": incident} if incident is not None else {}),
            "rule_decision": decision, "report": answer[:16000], "actions_applied": [],
            "notice": "Оценка SLO вычислена по метрикам. Текст модели не проверен и может противоречить данным. Ассистент не меняет кластер."}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/report", dependencies=[Depends(authorize_webhook)])
def report():
    return build_report(gather(os.getenv("TARGET_NAMESPACE", "demo")))


def incident_facts(document):
    """В модель передается белый список полей журнала, без сырых ошибок и ID оператора."""
    state = document.get("state") or {}
    history = state.get("_last_recovery") or {}
    def number(value):
        return value if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) else None
    start = number(history.get("started_at")) or number(state.get("_broke_at"))
    finish = number(history.get("slo_restored_at"))
    accepted = number(history.get("api_accepted_at"))
    end = number(history.get("resolved_received_at"))
    facts = {"scope": "latest_completed" if end else "current_or_unknown",
             "status": "completed" if end else "ongoing_or_unknown", "started_at": start,
             "slo_restored_at": finish, "api_accepted_at": accepted, "resolved_received_at": end,
             "mttr_seconds": finish - start if start is not None and finish is not None and finish >= start else None,
             "api_delay_seconds": accepted - start if start is not None and accepted is not None and accepted >= start else None,
             "operations": [], "timeline": [], "current_unknown_operation": state.get("_uncertain") is True}
    allowed_actions = {"scale", "scale_down", "delete_pod", "restart_redis", "rollback", "none"}
    allowed_statuses = {"prepared", "applied", "failed", "unknown", "observed_after_restart"}
    for operation in document.get("operations", [])[-100:]:
        stamp = number(operation.get("prepared_at"))
        if start is None or stamp is None or stamp < start or end is not None and stamp > end:
            continue
        if operation.get("action") not in allowed_actions or operation.get("status") not in allowed_statuses:
            continue
        facts["operations"].append({"action": operation["action"], "status": operation["status"],
                                    "prepared_at": stamp, "api_accepted_at": number(operation.get("api_accepted_at"))})
    allowed_events = {"decision", "approval_requested", "approval_callback", "approval_refused", "operation_unknown", "operation_observed", "incident_resolved"}
    for event in document.get("events", [])[-200:]:
        stamp = number(event.get("time"))
        if start is None or stamp is None or stamp < start or end is not None and stamp > end or event.get("event") not in allowed_events:
            continue
        item = {"time": stamp, "event": event["event"]}
        for key, values in {"action": allowed_actions, "diagnosis": {"redis", "partition", "slow_pod", "cpu", "bad_release", "unknown", "settling"},
                            "mode": {"auto", "approval", "ignore", "escalate"}, "choice": {"approve", "reject"},
                            "result": {"rejected", "ignored", "failed", "skipped", "dry-run rollback to revision", "dry-run restart redis; restart redis"}}.items():
            if event.get(key) in values:
                item[key] = event[key]
        facts["timeline"].append(item)
    facts["timeline_truncated"] = len(facts["timeline"]) > 12
    facts["unknown_operation_in_selected_incident"] = any(row["status"] in {"prepared", "unknown"} for row in facts["operations"])
    facts["operations_truncated"] = len(facts["operations"]) > 6
    facts["timeline"] = facts["timeline"][-12:]
    facts["operations"] = facts["operations"][-6:]
    return facts


def read_journal():
    try:
        return KubernetesStore().peek()
    except Exception:
        raise HTTPException(status_code=503, detail="Журнал healer недоступен; отчет об инциденте не создан") from None


@app.post("/postmortem", dependencies=[Depends(authorize_webhook)])
def postmortem():
    return build_report(gather(os.getenv("TARGET_NAMESPACE", "demo")), incident=incident_facts(read_journal()))
