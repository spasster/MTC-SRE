from prometheus_client import generate_latest

from app import main, observability as obs
from test_webhook import evidence, alert, FakeActuator


def test_uncertain_operation_has_separate_metrics_and_persistent_intent(monkeypatch):
    main.reset_state()
    main.actuator = FakeActuator()
    monkeypatch.setattr(main, "gather", lambda *args, **kwargs: evidence())
    original = main.actuator.delete_pod
    def timeout(*args, **kwargs):
        result = original(*args, **kwargs)
        if not kwargs.get("dry_run"):
            raise TimeoutError("URL с секретом")
        return result
    monkeypatch.setattr(main.actuator, "delete_pod", timeout)
    main.handle_alert(alert())
    rendered = generate_latest(obs.REGISTRY).decode()
    assert 'healer_operations_total{action="delete_pod",result="unknown"}' in rendered
    assert "healer_operation_unknown 1.0" in rendered
    assert "URL с секретом" not in rendered and "operator_key" not in rendered
    main.reset_state()


def test_incomplete_evidence_is_visible_instead_of_zero_age():
    obs.evidence({"error": "Нет данных", "telemetry_complete": False})
    rendered = generate_latest(obs.REGISTRY).decode()
    assert "healer_evidence_complete 0.0" in rendered
    assert "healer_evidence_age_seconds NaN" in rendered


def test_proven_diagnosis_replaces_a_provisional_unknown_title():
    from app.diagnose import Decision
    main.reset_state()
    main.remember_title(Decision("unknown", "escalate", "none", None, "Нет данных"))
    main.remember_title(Decision("redis", "approval", "restart_redis", "redis", "Таймаут"))
    assert main._incident_name == main.TITLES["redis"]
    main.reset_state()
