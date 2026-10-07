"""Отказы хранилища, смена лидера и восстановление незавершенных операций."""

import copy
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes.client.rest import ApiException

from app import main
from app.state import KubernetesStore, MemoryStore, StateUnavailable, empty_document
from test_webhook import FakeActuator, evidence, alert, HEALTHY


class Backend:
    def __init__(self):
        self.now = 1000
        self.lease = SimpleNamespace(metadata=SimpleNamespace(resource_version="1"), spec=None)
        self.map = SimpleNamespace(metadata=SimpleNamespace(resource_version="1"), data={})
        self.core = Mock()
        self.coordination = Mock()
        self.core.read_namespaced_config_map.side_effect = lambda *args, **kwargs: copy.deepcopy(self.map)
        self.core.patch_namespaced_config_map.side_effect = self.patch_map
        self.coordination.read_namespaced_lease.side_effect = lambda *args, **kwargs: copy.deepcopy(self.lease)
        self.coordination.patch_namespaced_lease.side_effect = self.patch_lease

    def patch_map(self, name, namespace, body, **kwargs):
        if body["metadata"]["resourceVersion"] != self.map.metadata.resource_version:
            raise ApiException(status=409)
        self.map.metadata.resource_version = str(int(self.map.metadata.resource_version) + 1)
        self.map.data.update(body["data"])
        self.map.metadata.annotations = body["metadata"].get("annotations", {})
        return copy.deepcopy(self.map)

    def patch_lease(self, name, namespace, body, **kwargs):
        if body["metadata"]["resourceVersion"] != self.lease.metadata.resource_version:
            raise ApiException(status=409)
        self.lease.metadata.resource_version = str(int(self.lease.metadata.resource_version) + 1)
        spec = body["spec"]
        self.lease.spec = SimpleNamespace(holder_identity=spec["holderIdentity"],
                                         renew_time=datetime.fromisoformat(spec["renewTime"]),
                                         lease_duration_seconds=spec["leaseDurationSeconds"])
        return copy.deepcopy(self.lease)

    def store(self, owner):
        return KubernetesStore(self.core, self.coordination, clock=lambda: self.now, owner=owner)


def valid_snapshot(actions=0):
    main.reset_state()
    snapshot = main.state_snapshot()
    snapshot["_actions"] = actions
    return snapshot


def test_only_one_holder_can_write_and_expired_holder_is_fenced():
    backend = Backend()
    first, second = backend.store("first"), backend.store("second")
    assert first.acquire()
    assert not second.acquire()
    first.load()
    first.save(valid_snapshot(1))
    backend.now += 31
    assert second.acquire()
    assert second.load()["_actions"] == 1
    with pytest.raises(StateUnavailable):
        first.save(valid_snapshot())
    assert json.loads(backend.map.data["state.json"])["state"]["_actions"] == 1


def test_compare_and_swap_conflict_does_not_overwrite_newer_state():
    backend = Backend()
    store = backend.store("holder")
    assert store.acquire()
    store.load()
    backend.map.metadata.resource_version = "9"
    with pytest.raises(StateUnavailable):
        store.save(valid_snapshot())
    assert backend.map.data == {}
    assert store.document["state"] == {}


@pytest.mark.parametrize("key,value", [
    ("_actions", -1), ("_actions", True), ("_actions", 1.5),
    ("_cooldown_until", float("nan")), ("_broke_at", -10), ("_fixed_at", float("inf")),
    ("_uncertain", "false"), ("_closed", "redis"), ("_recovery_targets", ["payment"]),
    ("_pending", {"action": "rollback"}), ("_scale_owner", {"uid": "only-uid"}),
])
def test_invalid_restored_snapshot_cannot_change_live_state(key, value):
    broken = valid_snapshot()
    broken[key] = value
    main._actions = 2
    before = copy.deepcopy(main.state_snapshot())
    with pytest.raises(StateUnavailable):
        main.restore_state(broken)
    assert main.state_snapshot() == before


@pytest.mark.parametrize("diagnosis,action,target", [("redis", "rollback", "checkout"), ("bad_release", "restart_redis", "redis")])
def test_contradictory_approval_cannot_restore_or_persist(diagnosis, action, target):
    snapshot = valid_snapshot()
    snapshot["_pending"] = {"diagnosis": diagnosis, "mode": "approval", "action": action, "target": target,
        "detail": "Управляемая проверка противоречия", "deployment_identity": {"uid": "controlled", "generation": 1, "revision": "1"},
        "approval_id": "controlled", "approval_expires_at": time.time() + 300}
    main._actions = 2
    before = copy.deepcopy(main.state_snapshot())
    with pytest.raises(StateUnavailable):
        main.restore_state(snapshot)
    assert main.state_snapshot() == before
    backend = Backend()
    store = backend.store("holder")
    store.acquire()
    store.load()
    with pytest.raises(StateUnavailable):
        store.save(snapshot)
    backend.core.patch_namespaced_config_map.assert_not_called()
    assert store.document["state"] == {}


@pytest.mark.parametrize("diagnosis,action,target", [("redis", "restart_redis", "redis"), ("bad_release", "rollback", "checkout")])
def test_consistent_approval_survives_restore(diagnosis, action, target):
    snapshot = valid_snapshot()
    snapshot["_pending"] = {"diagnosis": diagnosis, "mode": "approval", "action": action, "target": target,
        "detail": "Управляемая проверка сохранения", "deployment_identity": {"uid": "controlled", "generation": 1, "revision": "1"},
        "approval_id": "controlled", "approval_expires_at": time.time() + 300}
    main.restore_state(snapshot)
    assert (main._pending.diagnosis, main._pending.action, main._pending.target) == (diagnosis, action, target)


@pytest.mark.parametrize("encoded", ["null", "false", "[]", "{}"])
def test_empty_or_false_json_is_not_a_fresh_installation(encoded):
    backend = Backend()
    backend.map.data = {"state.json": encoded}
    store = backend.store("holder")
    store.acquire()
    with pytest.raises(StateUnavailable):
        store.load()
    backend.core.patch_namespaced_config_map.assert_not_called()


def test_missing_initialized_data_cannot_reset_the_budget():
    backend = Backend()
    store = backend.store("holder")
    store.acquire()
    store.load()
    store.save(valid_snapshot(2))
    backend.map.data = {}
    with pytest.raises(StateUnavailable):
        store.load()


def test_invalid_json_state_is_refused_on_read_and_before_write():
    backend = Backend()
    broken = empty_document()
    broken["state"] = valid_snapshot(-1)
    backend.map.data = {"state.json": json.dumps(broken)}
    store = backend.store("holder")
    store.acquire()
    with pytest.raises(StateUnavailable):
        store.load()
    backend.core.patch_namespaced_config_map.assert_not_called()
    backend.map.data = {}
    store.load()
    with pytest.raises(StateUnavailable):
        store.save(broken["state"])
    assert store.document["state"] == {}
    backend.core.patch_namespaced_config_map.assert_not_called()


def test_malformed_operation_does_not_reconcile_or_apply(controlled):
    snapshot = main.state_snapshot()
    snapshot["_action_intent"] = {"id": "operation", "action": "delete_pod", "target": "slow",
                                  "before": {"uid": "slow"}, "status": "prepared"}
    controlled.document["state"] = snapshot
    with pytest.raises(StateUnavailable):
        main.handle_alert(alert())
    assert main.actuator.calls == []


def test_missing_lease_or_broken_state_prevents_writes():
    backend = Backend()
    store = backend.store("holder")
    backend.coordination.read_namespaced_lease.side_effect = ApiException(status=403)
    with pytest.raises(StateUnavailable):
        store.acquire()
    backend.core.patch_namespaced_config_map.assert_not_called()
    backend = Backend()
    store = backend.store("holder")
    store.acquire()
    backend.map.data = {"state.json": "broken"}
    with pytest.raises(StateUnavailable):
        store.load()
    backend.core.patch_namespaced_config_map.assert_not_called()


class DurableMemory(MemoryStore):
    durable = True


@pytest.fixture
def controlled(monkeypatch):
    main.reset_state()
    store = DurableMemory()
    main._state_store = store
    main.actuator = FakeActuator()
    main.rules = main.load_rules()
    monkeypatch.setattr(main, "gather", lambda *args, **kwargs: evidence())
    yield store
    main.reset_state()


def test_worker_removes_restored_orphan_resolved_without_new_history(controlled, monkeypatch):
    snapshot = main.state_snapshot()
    snapshot["_resolved_pending"] = True
    snapshot["_mttr_sent"] = True
    controlled.document["state"] = snapshot
    unavailable = Mock(side_effect=RuntimeError("Метрики недоступны"))
    monkeypatch.setattr(main, "gather", unavailable)
    actor = main.actuator

    assert main.check_recovery() is None
    assert controlled.document["state"]["_resolved_pending"] is False
    assert controlled.document["state"]["_last_recovery"] is None
    assert controlled.document["events"] == [] and controlled.document["operations"] == []
    assert actor.calls == []
    unavailable.assert_not_called()
    main.reset_state()
    main._state_store, main.actuator = controlled, actor
    assert main.check_recovery() is None
    assert main._resolved_pending is False and main._broke_at is None
    unavailable.assert_not_called()


def test_restart_preserves_action_budget_and_cooldown(controlled):
    main.handle_alert(alert())
    assert main._actions == 1
    store, actor = controlled, main.actuator
    main.reset_state()
    main._state_store, main.actuator = store, actor
    result = main.handle_alert(alert("CheckoutSyntheticFailed"))
    assert result["result"] == "ignored: cooling down"
    assert main._actions == 1
    assert len(actor.calls) == 2


def test_worker_closes_recovered_incident_without_a_resolved_webhook(controlled, monkeypatch):
    monkeypatch.setattr(main.time, "time", lambda: 1000)
    main.handle_alert(alert())
    assert main._actions == 1 and main._recovery_pending and not main._resolved_pending
    actor = main.actuator
    monkeypatch.setattr(main, "gather", lambda *args, **kwargs: evidence(pods=list(HEALTHY)))
    monkeypatch.setattr(main.time, "time", lambda: 1025)

    assert main.check_recovery()["result"] == "resolved"
    saved = controlled.document["state"]
    assert saved["_broke_at"] is None and saved["_actions"] == 0
    assert not saved["_recovery_pending"] and not saved["_resolved_pending"]
    assert saved["_cooldown_until"] == 0 and not saved["_recovery_targets"]
    assert saved["_last_recovery"]["started_at"] == 1000
    assert saved["_last_recovery"]["slo_restored_at"] == 1025
    assert saved["_last_recovery"]["closed_at"] == 1025
    assert saved["_last_recovery"]["resolved_received_at"] is None
    assert len(actor.calls) == 2
    assert main.check_recovery() is None
    main.reset_state()
    main._state_store, main.actuator = controlled, actor
    main.check_recovery()
    assert main._broke_at is None and main._actions == 0
    assert controlled.document["operations"][0]["status"] == "applied"


@pytest.mark.parametrize("reason", ["missing_probe", "not_ready", "unknown_operation", "evidence_error"])
def test_worker_does_not_close_an_unconfirmed_recovery(controlled, monkeypatch, reason):
    main.handle_alert(alert())
    healthy = evidence(pods=list(HEALTHY))
    monkeypatch.setattr(main, "gather", lambda *args, **kwargs: healthy)
    if reason == "missing_probe":
        healthy["synthetic_success"] = None
    elif reason == "not_ready":
        monkeypatch.setattr(main.actuator, "deployment_ready", lambda *args: False)
    elif reason == "unknown_operation":
        main._uncertain = True
        main.persist_state()
    else:
        def unavailable(*args, **kwargs):
            raise TimeoutError("Чтение метрик не завершилось")
        monkeypatch.setattr(main, "gather", unavailable)
    if reason == "evidence_error":
        with pytest.raises(TimeoutError):
            main.check_recovery()
    else:
        assert main.check_recovery() is None
    saved = controlled.document["state"]
    assert saved["_broke_at"] is not None and saved["_actions"] == 1
    assert saved["_recovery_pending"] and saved["_last_recovery"] is None
    assert len(main.actuator.calls) == 2


@pytest.mark.parametrize("closed_at", [None, 1040])
def test_legacy_and_worker_recovery_history_survive_restore(closed_at):
    snapshot = valid_snapshot()
    history = {"incident": "Checkout", "started_at": 1000, "api_accepted_at": 1005,
               "slo_restored_at": 1035, "resolved_received_at": None if closed_at else 1040}
    if closed_at is not None:
        history["closed_at"] = closed_at
    snapshot["_last_recovery"] = history
    main.restore_state(snapshot)
    assert main.state_snapshot()["_last_recovery"] == history


def test_resolved_webhook_does_not_clear_an_unknown_operation(controlled, monkeypatch):
    main.handle_alert(alert())
    main._uncertain = True
    main.persist_state()
    monkeypatch.setattr(main, "gather", lambda *args, **kwargs: evidence(pods=list(HEALTHY)))
    assert main.handle_resolved()["result"] == "ignored: operation unknown"
    assert controlled.document["state"]["_actions"] == 1
    assert controlled.document["state"]["_broke_at"] is not None
    assert controlled.document["state"]["_resolved_pending"]


def test_uncertain_api_result_is_reserved_and_not_repeated_after_restart(controlled, monkeypatch):
    original = main.actuator.delete_pod

    def timeout(*args, **kwargs):
        result = original(*args, **kwargs)
        if not kwargs.get("dry_run"):
            raise TimeoutError("Ответ API потерян")
        return result

    monkeypatch.setattr(main.actuator, "delete_pod", timeout)
    assert main.handle_alert(alert())["result"].startswith("failed:")
    assert main._actions == 1 and main._uncertain
    store, actor = controlled, main.actuator
    store.document["state"]["_cooldown_until"] = 0
    main.reset_state()
    main._state_store, main.actuator = store, actor
    result = main.handle_alert(alert())
    assert result["result"].startswith("failed:")
    assert len(actor.calls) == 2
    assert main._actions == 1 and main._uncertain


def test_observed_operation_reconciles_without_repeating_the_api(controlled, monkeypatch):
    main._actions = 1
    main._action_intent = {"id": "operation", "action": "delete_pod", "target": "slow", "before": {"uid": "slow"},
                           "prepared_at": 1000, "status": "prepared"}
    controlled.document["operations"].append(main._action_intent.copy())
    main.persist_state()
    monkeypatch.setattr(main.actuator, "operation_observed", lambda *args: True)
    main.check_recovery()
    assert main._action_intent is None and not main._uncertain
    assert main._recovery_pending
    assert main.actuator.calls == []
    assert controlled.document["operations"][0]["status"] == "observed_after_restart"


def test_operator_revision_change_prevents_owned_scale_down(controlled, monkeypatch):
    main._scaled_by_healer = True
    main._scale_owner = main.actuator.deployment_identity("demo", "checkout")
    main.persist_state()
    main.actuator.generation += 1
    monkeypatch.setattr(main, "gather", lambda *args, **kwargs: evidence(replicas=5, p95=.05))
    result = main.handle_resolved()
    assert result["result"] == "ignored: operator changed replicas"
    assert main.actuator.calls == []
