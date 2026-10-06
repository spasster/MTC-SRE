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
POSTMORTEM_SECTIONS = {
    "symptoms": "Симптомы",
    "actions": "Действия и подтверждения",
    "recovery": "Восстановление и время",
    "next_checks": "Что еще проверить",
}
POSTMORTEM_SCHEMA = {
    "type": "object",
    "properties": {key: {"type": "string", "minLength": 1, "maxLength": 600}
                   for key in POSTMORTEM_SECTIONS},
    "required": list(POSTMORTEM_SECTIONS),
    "additionalProperties": False,
}


def factual_sections(assessment, incident):
    """Факты отчета формируются кодом; модель предлагает только следующую проверку."""
    states = {"healthy": "в норме", "violated": "нарушен", "unknown": "не подтвержден из-за отсутствия данных"}
    actions = {"scale": "масштабирование", "scale_down": "возврат масштаба", "delete_pod": "удаление пода",
               "restart_redis": "перезапуск Redis", "rollback": "откат", "none": "без вмешательства"}
    statuses = {"prepared": "намерение записано", "applied": "команда принята API", "failed": "ошибка команды",
                "unknown": "результат неизвестен", "observed_after_restart": "результат наблюдался после замены healer"}
    performed = [f"{actions.get(row.get('action'), 'неизвестное действие')}: {statuses.get(row.get('status'), 'неизвестный результат')}"
                 for row in (incident or {}).get("operations", [])]
    duration = (incident or {}).get("mttr_seconds")
    return {
        "symptoms": f"Сейчас SLO {states[assessment['state']]}; этот снимок описывает момент запроса, а не начало выбранного инцидента.",
        "actions": "; ".join(performed) if performed else "Операции Kubernetes в выбранном инциденте не зафиксированы.",
        "recovery": (f"По журналу восстановление SLO подтверждено; MTTR {duration:.1f} с."
                     if duration is not None else "Время восстановления SLO не подтверждено; MTTR не определен."),
    }


CHECKS = {
    "unknown_operation": (
        "Сопоставить намерение в журнале с UID пода или аннотацией mtc-sre/healer-operation перед повтором команды.",
        "Проверить неопределенную операцию и владельца Lease; до независимого подтверждения сохранить запрет повторного действия.",
    ),
    "cpu": (
        "Сопоставить p95 и CPU throttle по подам checkout до и после масштабирования.",
        "Проверить готовность реплик checkout и сохраненный момент подтверждения SLO после масштабирования.",
    ),
    "pod": (
        "Сверить UID удаленного и заменяющего подов checkout с журналом операции.",
        "Сопоставить p95 по подам checkout до удаления медленного пода и после готовности замены.",
    ),
    "redis": (
        "Сопоставить redis_error_ratio, готовность Redis и трейс ошибки во время инцидента.",
        "Сверить подтверждение оператора и журналы Redis перед выводом о последствиях рестарта для корзины.",
    ),
    "revision": (
        "Сверить UID Deployment checkout, текущую ревизию и журнал подтвержденного отката.",
        "Сопоставить application_error_ratio и p95 checkout до и после изменения ревизии.",
    ),
    "payment": (
        "Сопоставить payment_timeout_ratio и трейс checkout -> payment во время инцидента.",
        "Проверить доступность сети checkout -> payment и готовность payment перед изменением приложения.",
    ),
    "data": (
        "Сверить отказ от вмешательства с полнотой и свежестью метрик checkout в Prometheus в момент инцидента.",
        "Проверить up и метрики запросов checkout; отсутствие подтверждения восстановления сохранить как неизвестное время.",
    ),
    "slo": (
        "Сопоставить p95, долю ошибок и успешную синтетику checkout в одном временном окне.",
        "Проверить покрытие истории и бюджет SLO; короткий здоровый снимок не подтверждает месячный результат.",
    ),
}


def grounded_checks(snapshot, assessment, decision, incident):
    """Модель выбирает проверку только из кандидатов, связанных с входными фактами."""
    if incident is None:
        diagnoses, actions = {decision["diagnosis"]}, {decision["action"]}
        basis = {"source": "current_rules_and_metrics", "diagnosis": decision["diagnosis"], "slo_state": assessment["state"]}
    else:
        diagnoses = {row["diagnosis"] for row in incident.get("timeline", []) if row.get("diagnosis")}
        actions = {row["action"] for row in incident.get("operations", []) if row.get("action")}
        basis = {"source": "selected_incident", "diagnoses": sorted(diagnoses), "operations": sorted(actions),
                 "mttr_seconds": incident.get("mttr_seconds")}
    if incident and incident.get("unknown_operation_in_selected_incident"):
        group = "unknown_operation"
    elif "cpu" in diagnoses or actions & {"scale", "scale_down"}:
        group = "cpu"
    elif "slow_pod" in diagnoses or "delete_pod" in actions:
        group = "pod"
    elif "redis" in diagnoses or "restart_redis" in actions:
        group = "redis"
    elif "bad_release" in diagnoses or "rollback" in actions:
        group = "revision"
    elif diagnoses & {"partition", "payment"}:
        group = "payment"
    elif "unknown" in diagnoses or assessment["state"] == "unknown" or snapshot["collection_failed"]:
        group = "data"
    else:
        group = "slo"
    return [{"id": group + str(index), "group": group, "text": text, "basis": basis}
            for index, text in enumerate(CHECKS[group], 1)]


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
        "options": {"num_predict": 384, "num_ctx": 4096, "num_thread": 2, "temperature": 0.1},
        "keep_alive": "5m",
    }
    fixed_sections = factual_sections(assessment, incident)
    if incident is None:
        fixed_sections["actions"] = f"Решение правил по текущему снимку: {decision['diagnosis']}, {decision['mode']}; это гипотеза, а не история примененных действий."
        fixed_sections["recovery"] = "Текущий снимок не подтверждает время восстановления предыдущего инцидента; MTTR не определен."
    candidates = grounded_checks(snapshot, assessment, decision, incident)
    payload["format"] = {**POSTMORTEM_SCHEMA, "properties": {
        key: {"type": "string", "enum": [fixed_sections[key]] if key in fixed_sections else [row["text"] for row in candidates]}
        for key in POSTMORTEM_SECTIONS}}
    payload["prompt"] += "\nОбоснованные кандидаты следующей проверки: " + json.dumps(candidates, ensure_ascii=False)
    payload["prompt"] += "\nСхема ответа с неизменяемыми фактами: " + json.dumps(payload["format"], ensure_ascii=False)
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
        sections = json.loads(answer)
        if (not isinstance(sections, dict) or set(sections) != set(POSTMORTEM_SECTIONS)
            or any(not isinstance(value, str) or not value.strip() or len(value) > 600 for value in sections.values())):
            raise ValueError("Неверный формат отчета об инциденте")
        if any(sections[key] != value for key, value in fixed_sections.items()):
            raise ValueError("Текст модели изменил факты инцидента")
        selected = next((row for row in candidates if row["text"] == sections["next_checks"]), None)
        if selected is None:
            raise ValueError("Рекомендация не обоснована выбранными фактами")
        answer = "\n\n".join(f"{title}: {sections[key]}" for key, title in POSTMORTEM_SECTIONS.items())
    except (httpx.HTTPError, ValueError):
        raise HTTPException(status_code=503, detail="Локальная модель недоступна или ответ не завершён") from None
    finally:
        if own_client:
            http.close()
    return {"source": "local_llm", "model": payload["model"], "snapshot": snapshot, "slo_assessment": assessment,
            **({"incident_facts": incident} if incident is not None else {}),
            **({"report_sections": sections} if sections is not None else {}),
            "selected_recommendation": selected, "recommendation_candidates": candidates, "human_review_required": True,
            "rule_decision": decision, "report": answer[:16000], "actions_applied": [],
            "notice": "Факты вычислены кодом; модель выбирает одну обоснованную проверку из явных кандидатов. Это черновик для человека, без изменения кластера."}


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
