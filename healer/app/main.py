import json
import logging
import math
import os
import socket
import threading
import time
import uuid
from datetime import datetime, timezone

import yaml
from fastapi import FastAPI

from app.diagnose import decide
from app.evidence import gather
from app.k8s import ActionSkipped, K8sActuator
from app.telegram import Telegram

SERVICE = "healer"
WHITELIST = {"CheckoutSlow", "CheckoutHighErrorRate"}
TARGET_NAMESPACE = os.getenv("TARGET_NAMESPACE", "demo")

_lock = threading.Lock()
_busy = False
_actions = 0
_pending = None
_closed = set()
_escalated = False
_acted_alert = None
_cooldown_until = 0.0
_scale_down_pending = False
_broke_at = None
_fixed_at = None
_applied_at = None
_recovery_pending = False
_resolved_pending = False
_recovery_targets = set()
_recovery_worker = None
_worker_stop = threading.Event()
_incident_name = None
_mttr_sent = False
_poller_started = False
actuator = None
rules = None
notifier = None


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname.lower(),
            "service": SERVICE,
            "message": record.getMessage(),
        }
        for key in ("alertname", "diagnosis", "action", "result"):
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
app = FastAPI(title=SERVICE)


def rules_path() -> str:
    override = os.getenv("RULES_PATH")
    if override:
        return override
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "rules.yaml"))


def load_rules():
    with open(rules_path(), encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def get_rules():
    global rules
    if rules is None:
        rules = load_rules()
    return rules


def get_actuator():
    global actuator
    if actuator is None:
        actuator = K8sActuator()
    return actuator


def reset_state() -> None:
    global actuator, rules, notifier, _busy, _actions, _pending, _escalated, _acted_alert, _cooldown_until, _scale_down_pending
    global _broke_at, _fixed_at, _incident_name, _mttr_sent
    global _applied_at, _recovery_pending, _resolved_pending
    with _lock:
        _busy = False
        _actions = 0
        _pending = None
        _closed.clear()
        _escalated = False
        _acted_alert = None
        _cooldown_until = 0.0
        _scale_down_pending = False
        _broke_at = None
        _fixed_at = None
        _applied_at = None
        _recovery_pending = False
        _resolved_pending = False
        _recovery_targets.clear()
        _incident_name = None
        _mttr_sent = False
    actuator = None
    rules = None
    notifier = None


def get_notifier():
    global notifier
    if notifier is None:
        notifier = Telegram()
    return notifier


def _healthy(evidence, limits) -> bool:
    if not evidence or evidence.get("error"):
        return False
    error_ratio = evidence.get("error_ratio")
    if not _finite_number(error_ratio) or not 0 <= error_ratio <= limits["error_ratio"]:
        return False
    if "request_rate" in evidence:
        requests = evidence["request_rate"]
        if not _finite_number(requests) or requests <= 0:
            return False
    aggregate = evidence.get("p95")
    if aggregate is not None:
        return _finite_number(aggregate) and 0 <= aggregate < limits["slow_seconds"]
    pods = evidence.get("pods") or []
    if not pods:
        return False
    for pod in pods:
        latency = pod.get("p95")
        if not _finite_number(latency) or not 0 <= latency < limits["slow_seconds"]:
            return False
    return True


def _finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


TITLES = {
    "bad_release": "Плохой релиз checkout",
    "redis": "Redis не отвечает",
    "partition": "Нет связи checkout → payment",
    "slow_pod": "Один под тормозит",
    "cpu": "Не хватает CPU",
    "unknown": "Непонятная поломка",
}
WILL_DO = {
    "rollback": "сделаю rollout undo checkout",
    "restart_redis": "рестартую Deployment redis",
    "delete_pod": "удалю медленный под",
    "scale": "сделаю scale checkout",
    "none": "ничего не изменю",
}
HUMAN = {
    "bad_release": "Отказ оставит эту ревизию. Руками: sh chaos/inject.sh undo-release",
    "redis": "Пока думаешь, checkout может уже не читать корзину. Отказ оставит Redis как есть.",
    "partition": "Сними разрыв: sh chaos/inject.sh clear. Delete подов и scale не помогут.",
    "cpu": "Реплик уже 5. Дождись конца spike или удали Job k6-spike.",
    "slow_pod": "Обычно медленный под удаляю сам. Если не помогло: sh chaos/inject.sh clear",
    "unknown": "Дашборд «Инцидент» в Grafana и лог пода healer.",
}


def _title(decision) -> str:
    return TITLES.get(decision.diagnosis, decision.diagnosis)


def _parse_time(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    if "." in text:
        head, tail = text.split(".", 1)
        digits = []
        rest = ""
        for index, char in enumerate(tail):
            if char.isdigit():
                digits.append(char)
            else:
                rest = tail[index:]
                break
        text = head + "." + "".join(digits[:6]) + rest
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    minutes, secs = divmod(total, 60)
    if minutes and secs:
        return f"{minutes} мин {secs} с"
    if minutes:
        return f"{minutes} мин"
    return f"{secs} с"


def remember_start(alert) -> None:
    global _broke_at, _mttr_sent, _fixed_at, _incident_name
    if alert.get("status") == "firing" and _mttr_sent:
        _mttr_sent = False
        _broke_at = None
        _fixed_at = None
        _incident_name = None
    if alert.get("status") == "resolved" and _mttr_sent:
        return
    started = _parse_time(alert.get("startsAt"))
    if started is None and alert.get("status") == "firing" and _broke_at is None:
        started = time.time()
    if started is None:
        return
    if _broke_at is None or started < _broke_at:
        _broke_at = started


def remember_title(decision) -> None:
    global _incident_name
    if decision is None or decision.diagnosis == "settling" or _incident_name is not None:
        return
    _incident_name = _title(decision)


def mark_action_applied(deployment="checkout") -> None:
    global _applied_at, _recovery_pending, _fixed_at
    if _applied_at is None:
        _applied_at = time.time()
    _fixed_at = None
    _recovery_targets.update({"checkout", deployment})
    _recovery_pending = True


def mark_fixed() -> None:
    global _fixed_at
    if _fixed_at is None:
        _fixed_at = time.time()


def recovery_text(now=None) -> str:
    if _broke_at is None:
        return ""
    if _fixed_at is None and not _escalated:
        return ""
    moment = time.time() if now is None else now
    name = _incident_name or "Checkout"
    lines = ["✅ Поломка кончилась", "", f"{name}."]
    if _fixed_at is not None:
        if _applied_at is not None:
            lines.append(f"До принятия первой команды API {format_duration(_applied_at - _broke_at)}.")
        lines.append(f"До подтверждённого восстановления SLO {format_duration(_fixed_at - _broke_at)}.")
        lines.append(f"Сообщение resolved получено через {format_duration(moment - _broke_at)}.")
    else:
        lines.append("Сам не чинил.")
        lines.append(f"Алерт погас через {format_duration(moment - _broke_at)}.")
    return "\n".join(lines)


def notify_recovered() -> None:
    global _broke_at, _fixed_at, _incident_name, _mttr_sent
    text = recovery_text()
    if not text:
        return
    bot = get_notifier()
    if bot.enabled():
        bot.send(text)
    log.info(text.replace("\n", " "), extra={"result": "mttr"})
    _broke_at = None
    _fixed_at = None
    _incident_name = None
    _mttr_sent = True


def read_cart_seq():
    host = os.getenv("REDIS_HOST", "redis.demo.svc.cluster.local")
    port = int(os.getenv("REDIS_PORT", "6379"))
    query = b"*2\r\n$3\r\nGET\r\n$8\r\ncart:seq\r\n"
    try:
        with socket.create_connection((host, port), timeout=1) as sock:
            sock.settimeout(1)
            sock.sendall(query)
            data = b""
            while True:
                chunk = sock.recv(128)
                if not chunk:
                    break
                data += chunk
                if data.startswith(b"$-1") or data.startswith(b"-"):
                    break
                if data.startswith(b"$") and b"\r\n" in data:
                    line, _, rest = data.partition(b"\r\n")
                    size = int(line[1:])
                    if len(rest) >= size + 2:
                        break
    except (OSError, ValueError):
        return None
    if data.startswith(b"$-1"):
        return 0
    if not data.startswith(b"$"):
        return None
    line, _, rest = data.partition(b"\r\n")
    try:
        size = int(line[1:])
        return int(rest[:size])
    except ValueError:
        return None


def _cart_line(decision) -> str:
    seq = getattr(decision, "cart_seq", None)
    if seq is None:
        return "Рестарт сотрёт корзину. Номер прочитать не смог."
    return f"Рестарт сотрёт корзину. Сейчас в ней номер {seq}."


def approval_text(decision) -> str:
    if decision.diagnosis == "bad_release":
        return "\n".join([
            "⚠️ Нужен approve",
            "",
            "Плохой релиз checkout.",
            "Ревизия моложе 10 минут, 5xx пошли сразу, Redis и payment тихие.",
            "",
            "«Разрешить» — rollout undo на предыдущую ревизию.",
            "«Отклонить» — оставлю эту ревизию.",
        ])
    if decision.diagnosis == "redis":
        return "\n".join([
            "⚠️ Нужен approve",
            "",
            "Redis Ready, но checkout ловит таймаут.",
            _cart_line(decision),
            "Она только в памяти.",
            "",
            "«Разрешить» — рестарт redis.",
            "«Отклонить» — оставлю как есть.",
        ])
    return "\n".join([
        "⚠️ Нужно твоё решение",
        "",
        _title(decision),
        decision.detail,
        "",
        f"Если нажать «Разрешить», {WILL_DO.get(decision.action, decision.action)}.",
        "«Отклонить» оставит всё как есть.",
    ])


def request_approval(decision) -> str:
    global _pending
    if decision.diagnosis in _closed:
        return "ignored: already answered"
    if _pending is not None:
        if time.time() < _pending.approval_expires_at:
            return "ignored: waiting approval"
        _pending = None
    if decision.action == "restart_redis":
        decision.cart_seq = read_cart_seq()
    bot = get_notifier()
    if not bot.enabled():
        return f"needs approval: {decision.action}"
    decision.deployment_identity = get_actuator().deployment_identity(TARGET_NAMESPACE, decision.target)
    decision.approval_id = uuid.uuid4().hex
    approval_ttl = get_rules().get("approval_ttl_seconds", 300)
    decision.approval_expires_at = time.time() + approval_ttl
    _pending = decision
    start_poller()
    try:
        bot.send(
            approval_text(decision) + f"\n\nПодтверждение действует {format_duration(approval_ttl)}.",
            buttons=True,
            approval_id=decision.approval_id,
        )
    except Exception:
        _pending = None
        raise
    return "waiting approval"


def _escalation_text(decision) -> str:
    if decision.detail.startswith("Я уже дважды"):
        return "\n".join([
            "🚨 Дальше сам не делаю",
            "",
            "Уже два автоматических действия. Третье не делаю, дальше человек.",
        ])
    if decision.diagnosis == "partition":
        return "\n".join([
            "🚨 Сам не чиню",
            "",
            "Нет связи checkout → payment.",
            "Оба Ready. Delete пода и scale сеть не откроют.",
            "",
            "Сними разрыв: sh chaos/inject.sh clear",
        ])
    if decision.diagnosis == "cpu":
        return "\n".join([
            "⚠️ Scale до 5 не снял тормоза",
            "",
            "Прошло 2 минуты, GET /work всё ещё медленнее 300 мс.",
            "Шестую реплику не добавлю.",
        ])
    if decision.diagnosis == "unknown":
        return "\n".join([
            "🚨 Сам не чиню",
            "",
            decision.detail,
            "Ничего не менял.",
            "",
            "Дашборд «Инцидент» в Grafana и лог пода healer.",
        ])
    return "\n".join([
        "🚨 Сам не чиню",
        "",
        _title(decision),
        decision.detail,
        "",
        "Ничего не менял.",
    ])


def notify_escalation(decision) -> str:
    global _escalated
    if _escalated:
        return "escalated"
    bot = get_notifier()
    if bot.enabled():
        bot.send(_escalation_text(decision))
    _escalated = True
    return "escalated"


def apply_confirmed(decision) -> str:
    actor = get_actuator()
    current = decide(gather(TARGET_NAMESPACE, actuator=actor), get_rules())
    if (current.diagnosis, current.action, current.target) != (decision.diagnosis, decision.action, decision.target):
        raise ActionSkipped("Текущие метрики больше не подтверждают согласованное действие")
    guard = {"expected_identity": decision.deployment_identity}
    if decision.action == "rollback":
        preview = actor.rollback(TARGET_NAMESPACE, "checkout", dry_run=True, **guard)
        applied = actor.rollback(TARGET_NAMESPACE, "checkout", dry_run=False, **guard)
        mark_action_applied("checkout")
        return f"{preview}; {applied}"
    if decision.action == "restart_redis":
        preview = actor.restart(TARGET_NAMESPACE, "redis", dry_run=True, **guard)
        applied = actor.restart(TARGET_NAMESPACE, "redis", dry_run=False, **guard)
        mark_action_applied("redis")
        return f"{preview}; {applied}"
    return "escalated"


def apply_callback(data: str) -> str:
    global _busy
    with _lock:
        if _busy:
            return "ignored: busy"
        _busy = True
    try:
        return _apply_callback(data)
    finally:
        with _lock:
            _busy = False


def _apply_callback(data: str) -> str:
    global _pending, _actions
    if not isinstance(data, str):
        return "ignored: invalid callback"
    action, separator, approval_id = data.partition(":")
    if not separator or action not in {"approve", "reject"}:
        return "ignored: invalid callback"
    with _lock:
        decision = _pending
        if decision is None:
            return "ignored: nothing pending"
        if approval_id != decision.approval_id:
            return "ignored: stale approval"
        if time.time() >= decision.approval_expires_at:
            _pending = None
            return "ignored: expired approval"
        _pending = None
    bot = get_notifier()
    if action == "reject":
        with _lock:
            _closed.add(decision.diagnosis)
        if bot.enabled():
            bot.send("\n".join([
                "🚫 Отказ принят",
                "",
                f"{_title(decision)}. Ничего не менял.",
            ]))
        log.info("rejected", extra={"diagnosis": decision.diagnosis, "action": decision.action, "result": "rejected"})
        return "rejected"
    try:
        result = apply_confirmed(decision)
    except ActionSkipped as exc:
        result = f"skipped: {exc}"
        done = f"⏭ Действие пропущено\n\n{exc}. Кластер не изменён."
    except Exception as exc:
        result = f"failed: {exc}"
        done = f"⚠️ Действие не выполнено\n\n{_title(decision)}. Восстановление не подтверждено."
    else:
        with _lock:
            _actions += 1
            _closed.add(decision.diagnosis)
        if decision.action == "rollback":
            done = "\n".join([
                "✅ Откат checkout запрошен",
                "",
                "Kubernetes принял запрос на предыдущую ревизию. Жду восстановления запросов.",
            ])
        elif decision.action == "restart_redis":
            seq = getattr(decision, "cart_seq", None)
            wiped = "Рестарт удалит корзину в памяти."
            if seq is not None:
                wiped = f"Рестарт удалит корзину в памяти. Было {seq} на момент запроса подтверждения."
            done = "\n".join([
                "✅ Рестарт Redis запрошен",
                "",
                "Kubernetes принял запрос. Жду восстановления запросов.",
                wiped,
            ])
        else:
            done = "\n".join([
                "✅ Готово",
                "",
                _title(decision),
                f"{WILL_DO.get(decision.action, decision.action)}.",
            ])
    if bot.enabled():
        bot.send(done)
    log.info(decision.detail, extra={"diagnosis": decision.diagnosis, "action": decision.action, "result": result})
    return result


def _poll_loop() -> None:
    bot = get_notifier()
    while not _worker_stop.is_set():
        try:
            updates = bot.poll(timeout=25)
        except Exception:
            log.warning("Не удалось получить обновления Telegram")
            _worker_stop.wait(2)
            continue
        for callback_id, data in updates:
            try:
                bot.answer(callback_id)
            except Exception:
                log.warning("Не удалось подтвердить получение кнопки Telegram")
            try:
                apply_callback(data)
            except Exception:
                log.warning("Не удалось обработать кнопку Telegram; опрос продолжается")


def start_poller() -> None:
    global _poller_started
    bot = get_notifier()
    if _poller_started or not isinstance(bot, Telegram) or not bot.enabled():
        return
    _poller_started = True
    threading.Thread(target=_poll_loop, name="telegram", daemon=True).start()


def _recovery_loop() -> None:
    while not _worker_stop.wait(float(get_rules().get("recovery_poll_seconds", 5))):
        try:
            check_recovery()
            finish_scale_down()
        except Exception:
            log.exception("Не удалось проверить восстановление")


def start_recovery_worker() -> None:
    global _recovery_worker
    if _recovery_worker is not None and _recovery_worker.is_alive():
        return
    _worker_stop.clear()
    _recovery_worker = threading.Thread(target=_recovery_loop, name="recovery", daemon=True)
    _recovery_worker.start()


def recovery_ready(evidence, limits) -> bool:
    if not _healthy(evidence, limits):
        return False
    actor = get_actuator()
    return all(actor.deployment_ready(TARGET_NAMESPACE, name) for name in _recovery_targets | {"checkout"})


def check_recovery():
    global _busy
    with _lock:
        if _busy or not (_recovery_pending or _resolved_pending):
            return None
        _busy = True
    try:
        evidence = gather(TARGET_NAMESPACE, actuator=get_actuator())
        if not recovery_ready(evidence, get_rules()):
            return None
        if _applied_at is not None:
            mark_fixed()
        if _resolved_pending:
            return _handle_resolved(evidence=evidence)
        return {"result": "recovery confirmed"}
    finally:
        with _lock:
            _busy = False


def notify_done(decision) -> None:
    bot = get_notifier()
    if not bot.enabled():
        return
    if decision.action == "delete_pod":
        text = "\n".join([
            f"✅ Удалил под {decision.target}",
            "",
            "Он был минимум в 3 раза медленнее остальных, throttle CPU ниже 0.4.",
            "ReplicaSet поднимет новый под.",
        ])
    elif decision.action == "scale":
        text = "\n".join([
            f"✅ Scale checkout до {decision.target}",
            "",
            "Все поды медленные и упёрлись в CPU limit.",
            f"Реплик стало {decision.target}.",
        ])
    else:
        text = "\n".join([
            "✅ Готово",
            "",
            _title(decision),
            WILL_DO.get(decision.action, decision.action),
        ])
    bot.send(text)


def execute(decision) -> str:
    if decision.mode == "ignore":
        return "ignored: recovering"
    if decision.mode == "approval":
        return request_approval(decision)
    if decision.mode != "auto":
        return notify_escalation(decision)
    actor = get_actuator()
    if decision.action == "scale":
        replicas = int(decision.target)
        preview = actor.scale(TARGET_NAMESPACE, "checkout", replicas, dry_run=True)
        applied = actor.scale(TARGET_NAMESPACE, "checkout", replicas, dry_run=False)
        mark_action_applied("checkout")
        return f"{preview}; {applied}"
    if decision.action == "delete_pod":
        preview = actor.delete_pod(TARGET_NAMESPACE, decision.target, dry_run=True)
        applied = actor.delete_pod(TARGET_NAMESPACE, decision.target, dry_run=False)
        mark_action_applied("checkout")
        return f"{preview}; {applied}"
    return "escalated"


def note_action(decision) -> None:
    global _actions
    if decision.mode == "auto":
        _actions += 1


def arm_cooldown(alertname, limits) -> None:
    global _acted_alert, _cooldown_until
    _acted_alert = alertname
    _cooldown_until = time.time() + float(limits.get("cooldown_seconds", 120))


def _scale_to_baseline(evidence, limits) -> dict:
    replicas = evidence.get("replicas") or 0
    if replicas <= limits["scale_from"]:
        return {"result": "resolved"}
    actor = get_actuator()
    preview = actor.scale(TARGET_NAMESPACE, "checkout", limits["scale_from"], dry_run=True)
    applied = actor.scale(TARGET_NAMESPACE, "checkout", limits["scale_from"], dry_run=False)
    log.info("scaled down", extra={"action": "scale", "result": applied})
    bot = get_notifier()
    if bot.enabled():
        bot.send("\n".join([
            "✅ Scale checkout обратно до 2",
            "",
            "Spike кончился, GET /work снова быстрее 300 мс.",
            "Реплик снова 2.",
        ]))
    return {"action": "scale", "result": f"{preview}; {applied}"}


def handle_resolved(evidence=None) -> dict:
    global _busy
    with _lock:
        if _busy:
            return {"result": "ignored: busy"}
        _busy = True
    try:
        return _handle_resolved(evidence=evidence)
    finally:
        with _lock:
            _busy = False


def _handle_resolved(evidence=None) -> dict:
    global _actions, _pending, _escalated, _acted_alert, _cooldown_until, _scale_down_pending
    global _applied_at, _recovery_pending, _resolved_pending
    limits = get_rules()
    _resolved_pending = True
    try:
        if evidence is None:
            evidence = gather(TARGET_NAMESPACE, actuator=get_actuator())
        ready = recovery_ready(evidence, limits)
    except Exception as exc:
        return {"result": "ignored: evidence failed", "detail": str(exc)}
    if not ready:
        return {"result": "ignored: still unhealthy"}
    if _applied_at is not None:
        mark_fixed()
    notify_recovered()
    _applied_at = None
    _recovery_pending = False
    _resolved_pending = False
    _recovery_targets.clear()
    _actions = 0
    _pending = None
    _closed.clear()
    _escalated = False
    _acted_alert = None
    _cooldown_until = 0.0
    if get_actuator().spike_running(TARGET_NAMESPACE):
        _scale_down_pending = True
        log.info(
            "Spike ещё идёт, верну 2 реплики когда Job кончится",
            extra={"action": "scale", "result": "waiting for spike job"},
        )
        return {"result": "resolved", "detail": "spike still running"}
    _scale_down_pending = False
    return _scale_to_baseline(evidence, limits)


def finish_scale_down():
    global _scale_down_pending, _busy
    with _lock:
        if not _scale_down_pending or _busy:
            return None
        _busy = True
    try:
        limits = get_rules()
        if get_actuator().spike_running(TARGET_NAMESPACE):
            return None
        try:
            evidence = gather(TARGET_NAMESPACE, actuator=get_actuator())
        except Exception as exc:
            log.info("scale down check skipped", extra={"action": "scale", "result": str(exc)})
            return None
        if not recovery_ready(evidence, limits):
            return None
        result = _scale_to_baseline(evidence, limits)
        _scale_down_pending = False
        return result
    finally:
        with _lock:
            _busy = False


def handle_alert(alert: dict) -> dict:
    global _busy
    labels = alert.get("labels") or {}
    alertname = labels.get("alertname", "")
    service = labels.get("service", "")
    if service != "checkout":
        return {"alertname": alertname, "result": "ignored: service not allowed"}
    if alertname not in WHITELIST:
        return {"alertname": alertname, "result": "ignored: not in whitelist"}
    if alert.get("status") == "resolved":
        remember_start(alert)
        body = handle_resolved()
        body["alertname"] = alertname
        return body
    if alert.get("status") != "firing":
        return {"alertname": alertname, "result": "ignored: not firing"}
    remember_start(alert)

    limits = get_rules()
    with _lock:
        if _busy:
            return {"alertname": alertname, "result": "ignored: busy"}
        _busy = True
    try:
        evidence = gather(TARGET_NAMESPACE, actuator=get_actuator())
        decision = decide(evidence, limits)
        remember_title(decision)
        cooling = _acted_alert == alertname and time.time() < _cooldown_until
        if cooling and decision.mode != "approval" and decision.diagnosis != "partition":
            log.info(
                "уже лечил, жду окно",
                extra={"alertname": alertname, "diagnosis": decision.diagnosis, "action": "none", "result": "ignored: cooling down"},
            )
            return {
                "alertname": alertname,
                "diagnosis": decision.diagnosis,
                "action": "none",
                "mode": "ignore",
                "result": "ignored: cooling down",
                "detail": "уже лечил, жду окно",
            }
        if (
            _acted_alert == alertname
            and decision.diagnosis == "unknown"
        ):
            log.info(
                decision.detail,
                extra={"alertname": alertname, "diagnosis": decision.diagnosis, "action": "none", "result": "ignored: watching"},
            )
            return {
                "alertname": alertname,
                "diagnosis": decision.diagnosis,
                "action": "none",
                "mode": "ignore",
                "result": "ignored: watching",
                "detail": decision.detail,
            }
        if decision.mode == "auto" and _actions >= limits["max_actions"]:
            decision.mode = "escalate"
            decision.action = "none"
            decision.detail = "Я уже дважды чинил сам. Дальше пусть смотрит человек."
        try:
            result = execute(decision)
        except Exception as exc:
            result = f"failed: {exc}"
        else:
            if decision.mode == "auto":
                note_action(decision)
                arm_cooldown(alertname, limits)
                notify_done(decision)
        log.info(
            decision.detail,
            extra={"alertname": alertname, "diagnosis": decision.diagnosis, "action": decision.action, "result": result},
        )
        return {
            "alertname": alertname,
            "diagnosis": decision.diagnosis,
            "action": decision.action,
            "mode": decision.mode,
            "result": result,
            "detail": decision.detail,
        }
    finally:
        with _lock:
            _busy = False


@app.on_event("startup")
def _startup():
    start_recovery_worker()
    start_poller()


@app.on_event("shutdown")
def _shutdown():
    _worker_stop.set()
    if _recovery_worker is not None:
        _recovery_worker.join(timeout=1)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/webhook")
def webhook(payload: dict):
    alerts = payload.get("alerts") or []
    return {"results": [handle_alert(alert) for alert in alerts]}
