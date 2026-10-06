"""Отказы хранилища, смена лидера и восстановление незавершенных операций."""

import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes.client.rest import ApiException

from app import main
from app.state import KubernetesStore, MemoryStore, StateUnavailable, empty_document
from test_webhook import FakeActuator, evidence, alert


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
