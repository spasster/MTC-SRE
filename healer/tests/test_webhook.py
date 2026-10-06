import json
import logging

import pytest

from fastapi.testclient import TestClient

from app import main
from app.main import JsonFormatter, app

client = TestClient(app)

FAST = [
    {"pod": "fast", "p95": 0.04, "throttle": 0.0},
    {"pod": "slow", "p95": 2.0, "throttle": 0.05},
]
HOT = [
    {"pod": "a", "p95": 1.2, "throttle": 0.9},
    {"pod": "b", "p95": 1.1, "throttle": 0.8},
]


class FakeActuator:
    def __init__(self):
        self.calls = []
        self.spike = False

    def scale(self, namespace, name, replicas, dry_run=False):
        self.calls.append(("scale", namespace, name, replicas, dry_run))
        prefix = "dry-run " if dry_run else ""
        return f"{prefix}scale replicas={replicas}"

    def delete_pod(self, namespace, name, dry_run=False):
        self.calls.append(("delete", namespace, name, dry_run))
        prefix = "dry-run " if dry_run else ""
        return f"{prefix}delete pod {name}"

    def rollback(self, namespace, name, dry_run=False):
        self.calls.append(("rollback", namespace, name, dry_run))
        prefix = "dry-run " if dry_run else ""
        return f"{prefix}rollback {name}"

    def restart(self, namespace, name, dry_run=False):
        self.calls.append(("restart", namespace, name, dry_run))
        prefix = "dry-run " if dry_run else ""
        return f"{prefix}restart {name}"

    def spike_running(self, namespace):
        return self.spike

    def deployment_ready(self, namespace, name):
        return True


def evidence(**overrides):
    base = {
        "replicas": 2,
        "request_rate": 3.0,
        "revision_age_seconds": 5000,
        "error_ratio": 0.0,
        "application_error_ratio": 0.0,
        "payment_error_ratio": 0.0,
        "redis_error_ratio": 0.0,
        "payment_timeout_ratio": 0.0,
        "redis_ready": True,
        "payment_ready": True,
        "pods": list(FAST),
    }
    base.update(overrides)
    return base


def alert(name="CheckoutSlow", status="firing", service="checkout"):
    return {"status": status, "labels": {"alertname": name, "service": service}}


def setup_function():
    main.reset_state()
    main.actuator = FakeActuator()
    main.rules = main.load_rules()
    main.gather = lambda namespace, actuator=None: evidence()


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_slow_pod_deletes_after_dry_run(monkeypatch):
    monkeypatch.setattr(main, "gather", lambda namespace, actuator=None: evidence())
    response = client.post("/webhook", json={"alerts": [alert()]})
    body = response.json()["results"][0]
    assert body["diagnosis"] == "slow_pod"
    assert body["action"] == "delete_pod"
    assert main.actuator.calls == [
        ("delete", "demo", "slow", True),
        ("delete", "demo", "slow", False),
    ]


def test_cpu_scales_after_dry_run(monkeypatch):
    monkeypatch.setattr(main, "gather", lambda namespace, actuator=None: evidence(pods=list(HOT)))
    body = client.post("/webhook", json={"alerts": [alert()]}).json()["results"][0]
    assert body["diagnosis"] == "cpu"
    assert main.actuator.calls == [
        ("scale", "demo", "checkout", 5, True),
        ("scale", "demo", "checkout", 5, False),
    ]


def test_bad_release_does_not_change_the_cluster(monkeypatch):
    monkeypatch.setattr(
        main,
        "gather",
        lambda namespace, actuator=None: evidence(revision_age_seconds=30, error_ratio=0.8, application_error_ratio=0.8),
    )
    body = client.post("/webhook", json={"alerts": [alert("CheckoutHighErrorRate")]}).json()["results"][0]
    assert body["diagnosis"] == "bad_release"
    assert body["mode"] == "approval"
    assert body["result"] == "needs approval: rollback"
    assert main.actuator.calls == []


def test_partition_escalates(monkeypatch):
    monkeypatch.setattr(main, "gather", lambda namespace, actuator=None: evidence(payment_timeout_ratio=1))
    body = client.post("/webhook", json={"alerts": [alert("CheckoutHighErrorRate")]}).json()["results"][0]
    assert body["diagnosis"] == "partition"
    assert body["result"] == "escalated"
    assert main.actuator.calls == []


def test_action_label_is_ignored(monkeypatch):
    monkeypatch.setattr(main, "gather", lambda namespace, actuator=None: evidence())
    payload = alert()
    payload["labels"]["action"] = "restart"
    body = client.post("/webhook", json={"alerts": [payload]}).json()["results"][0]
    assert body["action"] == "delete_pod"


def test_third_auto_action_escalates(monkeypatch):
    monkeypatch.setattr(main, "gather", lambda namespace, actuator=None: evidence())
    client.post("/webhook", json={"alerts": [alert()]})
    main._cooldown_until = 0
    client.post("/webhook", json={"alerts": [alert()]})
    main._cooldown_until = 0
    body = client.post("/webhook", json={"alerts": [alert()]}).json()["results"][0]
    assert body["result"] == "escalated"
    assert body["detail"] == "Я уже дважды чинил сам. Дальше пусть смотрит человек."
    assert len(main.actuator.calls) == 4


def test_resolved_scales_back(monkeypatch):
    monkeypatch.setattr(
        main,
        "gather",
        lambda namespace, actuator=None: evidence(
            replicas=5,
            pods=[
                {"pod": "a", "p95": 0.05, "throttle": 0.0},
                {"pod": "b", "p95": 0.04, "throttle": 0.0},
            ],
        ),
    )
    body = client.post("/webhook", json={"alerts": [alert(status="resolved")]}).json()["results"][0]
    assert "scale replicas=2" in body["result"]
    assert main.actuator.calls[-1] == ("scale", "demo", "checkout", 2, False)


def test_resolved_does_not_scale_down_during_spike(monkeypatch):
    main.actuator.spike = True
    monkeypatch.setattr(
        main,
        "gather",
        lambda namespace, actuator=None: evidence(
            replicas=5,
            pods=[
                {"pod": "a", "p95": 0.05, "throttle": 0.0},
                {"pod": "b", "p95": 0.04, "throttle": 0.0},
            ],
        ),
    )
    body = client.post("/webhook", json={"alerts": [alert(status="resolved")]}).json()["results"][0]
    assert body["detail"] == "spike still running"
    assert main.actuator.calls == []
    assert main._scale_down_pending is True


def test_scale_down_runs_after_the_spike_job_ends(monkeypatch):
    main.notifier = FakeTelegram()
    main.actuator.spike = True
    monkeypatch.setattr(
        main,
        "gather",
        lambda namespace, actuator=None: evidence(
            replicas=5,
            pods=[
                {"pod": "a", "p95": 0.05, "throttle": 0.0},
                {"pod": "b", "p95": 0.04, "throttle": 0.0},
            ],
        ),
    )
    client.post("/webhook", json={"alerts": [alert(status="resolved")]})
    assert main.actuator.calls == []
    main.actuator.spike = False
    result = main.finish_scale_down()
    assert "scale replicas=2" in result["result"]
    assert main._scale_down_pending is False
    assert main.actuator.calls[-1] == ("scale", "demo", "checkout", 2, False)
    assert "обратно до 2" in main.notifier.messages[-1][0]


def test_scale_down_waits_while_answers_are_still_slow(monkeypatch):
    main.actuator.spike = True
    monkeypatch.setattr(
        main,
        "gather",
        lambda namespace, actuator=None: evidence(
            replicas=5,
            pods=[
                {"pod": "a", "p95": 0.05, "throttle": 0.0},
                {"pod": "b", "p95": 0.04, "throttle": 0.0},
            ],
        ),
    )
    client.post("/webhook", json={"alerts": [alert(status="resolved")]})
    main.actuator.spike = False
    monkeypatch.setattr(
        main,
        "gather",
        lambda namespace, actuator=None: evidence(
            replicas=5,
            pods=[
                {"pod": "a", "p95": 1.2, "throttle": 0.9},
                {"pod": "b", "p95": 1.1, "throttle": 0.8},
            ],
        ),
    )
    assert main.finish_scale_down() is None
    assert main._scale_down_pending is True
    assert main.actuator.calls == []


def test_scale_down_follows_the_aggregate(monkeypatch):
    main.notifier = FakeTelegram()
    main._scale_down_pending = True
    monkeypatch.setattr(
        main,
        "gather",
        lambda namespace, actuator=None: evidence(
            replicas=5,
            p95=0.12,
            pods=[
                {"pod": "a", "p95": 0.4, "throttle": 0.1},
                {"pod": "b", "p95": 0.05, "throttle": 0.0},
            ],
        ),
    )
    result = main.finish_scale_down()
    assert "scale replicas=2" in result["result"]
    assert main._scale_down_pending is False


def test_idle_pod_does_not_hide_a_failed_scale(monkeypatch):
    main.notifier = FakeTelegram()
    calls = {"n": 0}

    def gather(namespace, actuator=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return evidence(pods=list(HOT), replicas=2)
        return evidence(
            replicas=5,
            p95=2.0,
            pods=[
                {"pod": "idle", "p95": 0.1, "p95_recent": 0.1, "throttle": 0.03},
                {"pod": "hot", "p95": 5.0, "p95_recent": 5.0, "throttle": 0.98},
                {"pod": "new", "p95": 2.0, "p95_recent": 2.0, "throttle": 0.9},
            ],
        )

    monkeypatch.setattr(main, "gather", gather)
    client.post("/webhook", json={"alerts": [alert()]})
    main._cooldown_until = 0
    body = client.post("/webhook", json={"alerts": [alert()]}).json()["results"][0]
    assert body["result"] == "escalated"
    assert "Шестую реплику" in main.notifier.messages[-1][0]
    assert "300 мс" in main.notifier.messages[-1][0]


def test_ignore_other_service():
    response = client.post("/webhook", json={"alerts": [alert(service="obs")]})
    assert response.json()["results"][0]["result"] == "ignored: service not allowed"
    assert main.actuator.calls == []


class FakeTelegram:
    def __init__(self):
        self.messages = []

    def enabled(self):
        return True

    def send(self, text, buttons=False, approval_id=""):
        self.messages.append((text, buttons))


def test_approval_waits_for_a_button(monkeypatch):
    main.notifier = FakeTelegram()
    monkeypatch.setattr(
        main,
        "gather",
        lambda namespace, actuator=None: evidence(revision_age_seconds=30, error_ratio=0.8, application_error_ratio=0.8),
    )
    body = client.post("/webhook", json={"alerts": [alert("CheckoutHighErrorRate")]}).json()["results"][0]
    assert body["result"] == "waiting approval"
    assert main.actuator.calls == []
    assert main.notifier.messages[0][1] is True
    assert "корзин" not in main.notifier.messages[0][0]

    again = client.post("/webhook", json={"alerts": [alert("CheckoutHighErrorRate")]}).json()["results"][0]
    assert again["result"] == "ignored: waiting approval"
    assert len(main.notifier.messages) == 1

    result = main.apply_callback(f"approve:{main._pending.approval_id}")
    assert result.startswith("dry-run rollback")
    assert main.actuator.calls == [
        ("rollback", "demo", "checkout", True),
        ("rollback", "demo", "checkout", False),
    ]


def test_reject_does_not_change_redis(monkeypatch):
    main.notifier = FakeTelegram()
    monkeypatch.setattr(main, "read_cart_seq", lambda: 173377)
    monkeypatch.setattr(main, "gather", lambda namespace, actuator=None: evidence(redis_error_ratio=0.9))
    body = client.post("/webhook", json={"alerts": [alert("CheckoutHighErrorRate")]}).json()["results"][0]
    assert body["result"] == "waiting approval"
    assert "корзину" in main.notifier.messages[0][0]
    assert "173377" in main.notifier.messages[0][0]
    assert main.apply_callback(f"reject:{main._pending.approval_id}") == "rejected"
    assert main.actuator.calls == []
    later = client.post("/webhook", json={"alerts": [alert("CheckoutHighErrorRate")]}).json()["results"][0]
    assert later["result"] == "ignored: already answered"


def test_redis_restart_reports_the_wiped_number(monkeypatch):
    main.notifier = FakeTelegram()
    monkeypatch.setattr(main, "read_cart_seq", lambda: 173377)
    monkeypatch.setattr(main, "gather", lambda namespace, actuator=None: evidence(redis_error_ratio=0.9))
    client.post("/webhook", json={"alerts": [alert("CheckoutHighErrorRate")]})
    assert main.apply_callback(f"approve:{main._pending.approval_id}").startswith("dry-run restart")
    assert "Было 173377" in main.notifier.messages[-1][0]


HEALTHY = [
    {"pod": "a", "p95": 0.05, "p95_recent": 0.05, "throttle": 0.0},
    {"pod": "b", "p95": 0.04, "p95_recent": 0.04, "throttle": 0.0},
]


def test_recovery_text_counts_from_the_break_to_the_fix():
    main._broke_at = 1000
    main._fixed_at = 1075
    main._incident_name = "Плохой релиз checkout"
    text = main.recovery_text(now=1150)
    assert "До подтверждённого восстановления SLO 1 мин 15 с." in text
    assert "resolved получено через 2 мин 30 с." in text


def test_resolved_tells_how_long_the_fix_took(monkeypatch):
    main.notifier = FakeTelegram()
    monkeypatch.setattr(main, "gather", lambda namespace, actuator=None: evidence(pods=list(FAST)))
    firing = alert()
    firing["startsAt"] = "2026-10-05T10:25:28.419003119Z"
    client.post("/webhook", json={"alerts": [firing]})
    assert main._fixed_at is None
    assert main._applied_at is not None
    monkeypatch.setattr(main, "gather", lambda namespace, actuator=None: evidence(pods=list(HEALTHY)))
    resolved = alert(status="resolved")
    resolved["startsAt"] = firing["startsAt"]
    client.post("/webhook", json={"alerts": [resolved]})
    text = main.notifier.messages[-1][0]
    assert "Поломка кончилась" in text
    assert "До подтверждённого восстановления SLO" in text
    assert "resolved получено через" in text
    assert main.notifier.messages[-1][1] is False
    client.post("/webhook", json={"alerts": [resolved]})
    assert len([item for item in main.notifier.messages if "Поломка кончилась" in item[0]]) == 1


def test_partition_reports_only_the_resolved_time(monkeypatch):
    main.notifier = FakeTelegram()
    monkeypatch.setattr(
        main,
        "gather",
        lambda namespace, actuator=None: evidence(payment_timeout_ratio=0.8, pods=list(HEALTHY)),
    )
    firing = alert()
    firing["startsAt"] = "2026-10-05T10:30:51Z"
    client.post("/webhook", json={"alerts": [firing]})
    assert main.actuator.calls == []
    monkeypatch.setattr(main, "gather", lambda namespace, actuator=None: evidence(pods=list(HEALTHY)))
    resolved = alert(status="resolved")
    resolved["startsAt"] = firing["startsAt"]
    client.post("/webhook", json={"alerts": [resolved]})
    text = main.notifier.messages[-1][0]
    assert "Сам не чинил." in text
    assert "Алерт погас через" in text
    assert "До починки" not in text


def test_recovered_pods_do_not_page(monkeypatch):
    main.notifier = FakeTelegram()
    monkeypatch.setattr(
        main,
        "gather",
        lambda namespace, actuator=None: evidence(
            pods=[
                {"pod": "a", "p95": 2.0, "p95_recent": 0.05, "throttle": 0.0},
                {"pod": "b", "p95": 0.04, "p95_recent": 0.04, "throttle": 0.0},
            ]
        ),
    )
    body = client.post("/webhook", json={"alerts": [alert()]}).json()["results"][0]
    assert body["result"] == "ignored: recovering"
    assert main.actuator.calls == []
    assert main.notifier.messages == []


def test_repeat_while_cooling_down_stays_quiet(monkeypatch):
    main.notifier = FakeTelegram()
    monkeypatch.setattr(main, "gather", lambda namespace, actuator=None: evidence())
    client.post("/webhook", json={"alerts": [alert()]})
    again = client.post("/webhook", json={"alerts": [alert()]}).json()["results"][0]
    assert again["result"] == "ignored: cooling down"
    assert len(main.actuator.calls) == 2
    assert len(main.notifier.messages) == 1


def test_after_cooldown_a_failed_scale_is_reported(monkeypatch):
    main.notifier = FakeTelegram()
    calls = {"n": 0}

    def gather(namespace, actuator=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return evidence(pods=list(HOT), replicas=2)
        return evidence(pods=list(HOT), replicas=5)

    monkeypatch.setattr(main, "gather", gather)
    client.post("/webhook", json={"alerts": [alert()]})
    main._cooldown_until = 0
    body = client.post("/webhook", json={"alerts": [alert()]}).json()["results"][0]
    assert body["result"] == "escalated"
    assert "Шестую реплику" in main.notifier.messages[-1][0]


def test_auto_delete_tells_telegram(monkeypatch):
    main.notifier = FakeTelegram()
    monkeypatch.setattr(main, "gather", lambda namespace, actuator=None: evidence())
    body = client.post("/webhook", json={"alerts": [alert()]}).json()["results"][0]
    assert body["action"] == "delete_pod"
    text, buttons = main.notifier.messages[0]
    assert buttons is False
    assert "Удалил под" in text
    assert "slow" in text


def test_escalation_is_one_message(monkeypatch):
    main.notifier = FakeTelegram()
    monkeypatch.setattr(main, "gather", lambda namespace, actuator=None: evidence(payment_timeout_ratio=1))
    first = client.post("/webhook", json={"alerts": [alert("CheckoutHighErrorRate")]}).json()["results"][0]
    second = client.post("/webhook", json={"alerts": [alert("CheckoutHighErrorRate")]}).json()["results"][0]
    assert first["result"] == "escalated"
    assert second["result"] == "escalated"
    assert len(main.notifier.messages) == 1
    assert main.notifier.messages[0][1] is False
    assert main.actuator.calls == []


def test_log_line(monkeypatch, caplog):
    monkeypatch.setattr(main, "gather", lambda namespace, actuator=None: evidence())
    with caplog.at_level(logging.INFO):
        client.post("/webhook", json={"alerts": [alert()]})
    record = next(item for item in caplog.records if getattr(item, "diagnosis", None) == "slow_pod")
    payload = json.loads(JsonFormatter().format(record))
    assert payload["action"] == "delete_pod"
    assert "delete pod slow" in payload["result"]


def test_old_button_does_not_confirm_a_new_incident(monkeypatch):
    from app.diagnose import Decision

    main.notifier = FakeTelegram()
    main.request_approval(Decision("bad_release", "approval", "rollback", "checkout", "Первый инцидент"))
    old_id = main._pending.approval_id
    monkeypatch.setattr(main, "gather", lambda namespace, actuator=None: evidence(pods=list(HEALTHY)))
    main.handle_resolved()
    monkeypatch.setattr(main, "read_cart_seq", lambda: None)
    main.request_approval(Decision("redis", "approval", "restart_redis", "redis", "Второй инцидент"))
    current_id = main._pending.approval_id

    assert main.apply_callback(f"approve:{old_id}") == "ignored: stale approval"
    assert main._pending.approval_id == current_id
    assert main.actuator.calls == []
    assert main.apply_callback(f"approve:{current_id}").startswith("dry-run restart")


def test_expired_button_does_not_change_the_cluster(monkeypatch):
    from app.diagnose import Decision

    main.notifier = FakeTelegram()
    main.request_approval(Decision("bad_release", "approval", "rollback", "checkout", "Истёкший запрос"))
    pending = main._pending
    monkeypatch.setattr(main.time, "time", lambda: pending.approval_expires_at + 1)

    assert main.apply_callback(f"approve:{pending.approval_id}") == "ignored: expired approval"
    assert main.actuator.calls == []
    assert main._pending is None


def test_callback_without_request_id_is_not_an_approval():
    from app.diagnose import Decision

    main.notifier = FakeTelegram()
    main.request_approval(Decision("bad_release", "approval", "rollback", "checkout", "Текущий запрос"))
    assert main.apply_callback("approve") == "ignored: invalid callback"
    assert main._pending is not None
    assert main.actuator.calls == []


def test_failed_rollback_does_not_send_success_or_close_the_incident(monkeypatch):
    from app.diagnose import Decision

    main.notifier = FakeTelegram()
    main.request_approval(Decision("bad_release", "approval", "rollback", "checkout", "Проверка отказа API"))
    callback = f"approve:{main._pending.approval_id}"

    def unavailable(*args, **kwargs):
        raise RuntimeError("API недоступен")

    monkeypatch.setattr(main.actuator, "rollback", unavailable)
    assert main.apply_callback(callback).startswith("failed:")
    assert "Действие не выполнено" in main.notifier.messages[-1][0]
    assert main._fixed_at is None
    assert main._actions == 0
    assert "bad_release" not in main._closed


def test_skipped_rollback_does_not_mark_recovery(monkeypatch):
    from app.diagnose import Decision
    from app.k8s import ActionSkipped

    main.notifier = FakeTelegram()
    main.request_approval(Decision("bad_release", "approval", "rollback", "checkout", "Первый релиз"))
    callback = f"approve:{main._pending.approval_id}"

    def no_revision(*args, **kwargs):
        raise ActionSkipped("Нет предыдущей ревизии")

    monkeypatch.setattr(main.actuator, "rollback", no_revision)
    assert main.apply_callback(callback).startswith("skipped:")
    assert "Действие пропущено" in main.notifier.messages[-1][0]
    assert main._fixed_at is None
    assert main._actions == 0


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), -0.1])
def test_missing_or_invalid_error_rate_is_not_recovery(value):
    assert not main._healthy(evidence(error_ratio=value, pods=list(HEALTHY)), main.rules)


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), -1])
def test_missing_or_invalid_latency_is_not_recovery(value):
    assert not main._healthy(evidence(p95=value, pods=[]), main.rules)


@pytest.mark.parametrize("value", [None, 0, float("nan"), float("inf"), -1])
def test_recovery_requires_observed_requests(value):
    assert not main._healthy(evidence(request_rate=value, pods=list(HEALTHY)), main.rules)


def test_empty_metrics_do_not_close_or_scale_down_an_incident(monkeypatch):
    from app.evidence import gather
    from test_evidence import Actor

    main._actions = 1
    main._pending = object()
    missing = gather("demo", actuator=Actor(), query=lambda query: [])
    monkeypatch.setattr(main, "gather", lambda namespace, actuator=None: missing)
    assert main.handle_resolved()["result"] == "ignored: still unhealthy"
    assert main._pending is not None
    assert main._actions == 1
    assert main.actuator.calls == []


def test_mttr_waits_for_metrics_and_rollout(monkeypatch):
    from app.diagnose import Decision

    monkeypatch.setattr(main.time, "time", lambda: 1005)
    main._broke_at = 1000
    main.execute(Decision("cpu", "auto", "scale", "5", "Нагрузка"))
    assert main._applied_at == 1005
    assert main._fixed_at is None

    monkeypatch.setattr(main.time, "time", lambda: 1010)
    monkeypatch.setattr(main, "gather", lambda namespace, actuator=None: evidence(pods=list(HOT)))
    assert main.check_recovery() is None
    assert main._fixed_at is None

    monkeypatch.setattr(main, "gather", lambda namespace, actuator=None: evidence(pods=list(HEALTHY)))
    monkeypatch.setattr(main.actuator, "deployment_ready", lambda namespace, name: False)
    assert main.check_recovery() is None
    assert main._fixed_at is None

    monkeypatch.setattr(main.time, "time", lambda: 1025)
    monkeypatch.setattr(main.actuator, "deployment_ready", lambda namespace, name: True)
    assert main.check_recovery()["result"] == "recovery confirmed"
    assert main._fixed_at == 1025
    assert "До принятия первой команды API 5 с." in main.recovery_text(now=1030)
    assert "До подтверждённого восстановления SLO 25 с." in main.recovery_text(now=1030)


def test_resolved_before_rollout_is_rechecked_by_worker(monkeypatch):
    from app.diagnose import Decision

    main.notifier = FakeTelegram()
    main._broke_at = main.time.time() - 10
    main.execute(Decision("cpu", "auto", "scale", "5", "Нагрузка"))
    monkeypatch.setattr(main, "gather", lambda namespace, actuator=None: evidence(pods=list(HEALTHY)))
    monkeypatch.setattr(main.actuator, "deployment_ready", lambda namespace, name: False)
    assert main.handle_resolved()["result"] == "ignored: still unhealthy"
    assert main._fixed_at is None
    monkeypatch.setattr(main.actuator, "deployment_ready", lambda namespace, name: True)
    assert main.check_recovery()["result"] == "resolved"
    assert main._resolved_pending is False
    assert any("подтверждённого восстановления" in message for message, _ in main.notifier.messages)


def test_recovery_worker_starts_without_telegram():
    from app.telegram import Telegram

    main.notifier = Telegram(token="", chat_id="")
    try:
        main._startup()
        assert main._recovery_worker.is_alive()
    finally:
        main._shutdown()
    assert not main._recovery_worker.is_alive()
