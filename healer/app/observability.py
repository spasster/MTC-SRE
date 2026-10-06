"""Метрики контура восстановления без ID операторов и содержимого уведомлений."""

import time

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server

REGISTRY = CollectorRegistry()
DECISIONS = Counter("healer_decisions_total", "Решения по диагнозу и режиму", ["diagnosis", "mode"], registry=REGISTRY)
OPERATIONS = Counter("healer_operations_total", "Команды Kubernetes и результат API", ["action", "result"], registry=REGISTRY)
FAILURES = Counter("healer_failures_total", "Ошибки зависимостей контура", ["component"], registry=REGISTRY)
LEADER = Gauge("healer_leader", "Этот процесс владеет Lease", registry=REGISTRY)
STATE_OK = Gauge("healer_state_available", "Состояние прочитано и сохранено", registry=REGISTRY)
LAST_CHECK = Gauge("healer_state_checked_timestamp_seconds", "Время последней проверки состояния", registry=REGISTRY)
LAST_EVIDENCE = Gauge("healer_evidence_checked_timestamp_seconds", "Время последнего сбора метрик", registry=REGISTRY)
EVIDENCE_OK = Gauge("healer_evidence_complete", "Последний снимок полный", registry=REGISTRY)
EVIDENCE_AGE = Gauge("healer_evidence_age_seconds", "Возраст последнего полного снимка", registry=REGISTRY)
UNKNOWN = Gauge("healer_operation_unknown", "Есть команда с неопределенным результатом", registry=REGISTRY)
APPROVAL = Gauge("healer_approval_pending", "Ожидается решение оператора", registry=REGISTRY)
BUDGET = Gauge("healer_incident_actions", "Зарезервированные действия текущего инцидента", registry=REGISTRY)
RECOVERY = Histogram("healer_recovery_seconds", "Время до подтвержденного восстановления SLO", buckets=(15, 30, 60, 120, 180, 300, 600), registry=REGISTRY)
CHAOS_OBSERVED = Gauge("healer_chaos_injection_observed", "Chaos Mesh подтвердил инъекцию разрешенного теста", ["fault"], registry=REGISTRY)
_server = None
_last_evidence_at = 0


def start():
    global _server
    if _server is None:
        _server = start_http_server(8081, registry=REGISTRY)


def evidence(sample):
    global _last_evidence_at
    _last_evidence_at = time.time()
    LAST_EVIDENCE.set(_last_evidence_at)
    complete = sample.get("telemetry_complete") is True and not sample.get("error")
    EVIDENCE_OK.set(int(complete))
    EVIDENCE_AGE.set(sample.get("telemetry_age_seconds") or 0 if complete else float("nan"))
    if not complete:
        FAILURES.labels("evidence").inc()


def needs_sample():
    return time.time() - _last_evidence_at >= 15


def state(snapshot):
    UNKNOWN.set(int(bool(snapshot.get("_uncertain"))))
    APPROVAL.set(int(bool(snapshot.get("_pending"))))
    BUDGET.set(snapshot.get("_actions", 0))


def chaos(observed):
    for fault in ("pod", "network", "cpu"):
        CHAOS_OBSERVED.labels(fault).set(int(observed[fault]) if observed is not None else float("nan"))
