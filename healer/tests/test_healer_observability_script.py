"""Пауза сбора сохраняет UID ServiceMonitor и чужие изменения при очистке."""
import copy
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
spec = importlib.util.spec_from_file_location("healer_observability_script", Path(__file__).resolve().parents[2] / "scripts/verify-healer-observability.py")
observability = importlib.util.module_from_spec(spec)
spec.loader.exec_module(observability)


def controlled_monitor(monkeypatch):
    current = {"metadata": {"uid": "original-monitor", "resourceVersion": "10", "generation": 1,
                            "annotations": {"operator-note": "preserved"}},
               "spec": {"selector": {"matchLabels": {"app": "checkout"}}, "endpoints": [{"port": "metrics"}]}}
    writes = []
    monkeypatch.setattr(observability, "resource", lambda *args: copy.deepcopy(current))
    def patch(*args, **kwargs):
        writes.append((args, kwargs))
        assert "--type=json" in args and kwargs["timeout"] == 30
        operations = json.loads(args[args.index("-p") + 1])
        assert operations[0] == {"op": "test", "path": "/metadata/uid", "value": current["metadata"]["uid"]}
        assert operations[1] == {"op": "test", "path": "/metadata/resourceVersion", "value": current["metadata"]["resourceVersion"]}
        current["spec"] = copy.deepcopy(operations[2]["value"])
        current["metadata"]["annotations"] = copy.deepcopy(operations[3]["value"])
        current["metadata"]["generation"] += 1
        current["metadata"]["resourceVersion"] = str(int(current["metadata"]["resourceVersion"]) + 1)
        return SimpleNamespace(returncode=0, stdout=json.dumps(current), stderr="")
    monkeypatch.setattr(observability, "run_command", patch)
    return current, writes, patch


def test_monitor_pause_and_restore_preserve_uid_and_operator_annotations(monkeypatch):
    current, writes, _ = controlled_monitor(monkeypatch)
    original = copy.deepcopy(current)
    owner = observability.OwnedMonitor(original, "this-run")
    owner.pause()
    assert current["spec"]["selector"] == {"matchLabels": {"mtc-sre/monitor-paused": "this-run"}}
    assert current["metadata"]["uid"] == "original-monitor"
    owner.cleanup()
    assert current["spec"] == original["spec"] and current["metadata"]["uid"] == "original-monitor"
    assert current["metadata"]["annotations"] == {"operator-note": "preserved"} and len(writes) == 2


@pytest.mark.parametrize("change", ["uid", "generation", "selector", "marker"])
def test_monitor_cleanup_preserves_a_foreign_change(monkeypatch, change):
    current, writes, _ = controlled_monitor(monkeypatch)
    owner = observability.OwnedMonitor(copy.deepcopy(current), "this-run")
    owner.pause()
    if change == "selector":
        current["spec"]["selector"] = {"matchLabels": {"app": "operator"}}
    elif change == "marker":
        current["metadata"]["annotations"][observability.OWNER_ANNOTATION] = "other-run"
    elif change == "uid":
        current["metadata"]["uid"] = "replacement"
    else:
        current["metadata"]["generation"] += 1
    before = copy.deepcopy(current)
    with pytest.raises(RuntimeError, match="запрещена"):
        owner.cleanup()
    assert current == before and len(writes) == 1


def test_unknown_monitor_write_is_not_repeated_or_restored(monkeypatch):
    current, writes, _ = controlled_monitor(monkeypatch)
    def unknown(*args, **kwargs):
        writes.append((args, kwargs))
        raise RuntimeError("Ответ API потерян")
    monkeypatch.setattr(observability, "run_command", unknown)
    owner = observability.OwnedMonitor(copy.deepcopy(current), "this-run")
    with pytest.raises(RuntimeError, match="потерян"):
        owner.pause()
    with pytest.raises(RuntimeError, match="неизвестен"):
        owner.cleanup()
    assert len(writes) == 1


def test_explicit_conflict_rechecks_uid_before_any_repeat(monkeypatch):
    current, writes, _ = controlled_monitor(monkeypatch)
    def conflict(*args, **kwargs):
        writes.append((args, kwargs))
        current["metadata"]["uid"] = "replacement"
        return SimpleNamespace(returncode=1, stdout="", stderr="Error from server (Conflict)")
    monkeypatch.setattr(observability, "run_command", conflict)
    owner = observability.OwnedMonitor(copy.deepcopy(current), "this-run")
    with pytest.raises(RuntimeError, match="запрещена"):
        owner.pause()
    owner.cleanup()
    assert len(writes) == 1 and current["metadata"]["uid"] == "replacement"


def test_monitor_rejects_an_incomplete_success_response(monkeypatch):
    current, writes, _ = controlled_monitor(monkeypatch)
    def ignored(*args, **kwargs):
        writes.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(current), stderr="")
    monkeypatch.setattr(observability, "run_command", ignored)
    owner = observability.OwnedMonitor(copy.deepcopy(current), "this-run")
    with pytest.raises(RuntimeError, match="не подтвердил"):
        owner.pause()
    with pytest.raises(RuntimeError, match="неизвестен"):
        owner.cleanup()
    assert len(writes) == 1


def test_monitor_condition_after_deadline_is_rejected(monkeypatch):
    clock = [0]
    monkeypatch.setattr(observability.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(observability.time, "sleep", lambda *args: None)
    def delayed():
        clock[0] = 6
        return {"evidence_complete": 1}
    monkeypatch.setattr(observability, "sample", delayed)
    with pytest.raises(RuntimeError, match="в срок"):
        observability.wait(lambda row: True, timeout=5)


def incident_completion(monkeypatch, acknowledgement):
    calls = []
    monkeypatch.setattr(observability, "wait_healthy", lambda *args, **kwargs: calls.append("healthy"))
    def webhook(request, timeout):
        assert json.loads(request.data)["alerts"][0]["status"] == "resolved" and timeout == 30
        calls.append("webhook")
        return io.StringIO(json.dumps({"results": [{"result": acknowledgement}]}))
    monkeypatch.setattr(observability, "urlopen", webhook)
    def closed(**kwargs):
        assert kwargs["timeout"] == 180
        calls.append("closed-state")
        return {"incident_closed": True}
    monkeypatch.setattr(observability, "wait_incident_closed", closed)
    return calls


@pytest.mark.parametrize("acknowledgement", ["resolved", "ignored: no active incident"])
def test_completion_checks_saved_state_for_worker_and_webhook_closure(monkeypatch, acknowledgement):
    calls = incident_completion(monkeypatch, acknowledgement)
    payload = {"alerts": [{"status": "firing"}]}
    report = {}
    observability.close_test_incident(payload, "test-only", report)
    assert calls == ["healthy", "webhook", "closed-state"]
    assert report["test_incident_closed"] is True and report["closed_state"]["incident_closed"] is True
    assert report["resolved_webhook_result"][0]["result"] == acknowledgement
    assert payload["alerts"][0]["status"] == "firing"


@pytest.mark.parametrize("acknowledgement", ["resolved", "ignored: no active incident"])
def test_webhook_acknowledgement_does_not_replace_saved_closure(monkeypatch, acknowledgement):
    incident_completion(monkeypatch, acknowledgement)
    def still_active(**kwargs):
        raise RuntimeError("Сохраненный инцидент еще активен")
    monkeypatch.setattr(observability, "wait_incident_closed", still_active)
    report = {}
    with pytest.raises(RuntimeError, match="еще активен"):
        observability.close_test_incident({"alerts": [{"status": "firing"}]}, "test-only", report)
    assert report["test_incident_closed"] is False and "closed_state" not in report


def test_completion_without_healthy_evidence_does_not_send_resolved(monkeypatch):
    calls = incident_completion(monkeypatch, "resolved")
    def unknown_health(*args, **kwargs):
        raise RuntimeError("Здоровое состояние не подтверждено")
    monkeypatch.setattr(observability, "wait_healthy", unknown_health)
    report = {}
    with pytest.raises(RuntimeError, match="не подтверждено"):
        observability.close_test_incident({"alerts": [{"status": "firing"}]}, "test-only", report)
    assert calls == [] and report["test_incident_closed"] is False


def test_completion_rejects_an_unexpected_webhook_result(monkeypatch):
    calls = incident_completion(monkeypatch, "pending: unhealthy")
    report = {}
    with pytest.raises(RuntimeError, match="не подтвердил обработку"):
        observability.close_test_incident({"alerts": [{"status": "firing"}]}, "test-only", report)
    assert calls == ["healthy", "webhook"] and report["test_incident_closed"] is False
