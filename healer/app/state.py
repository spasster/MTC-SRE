"""Состояние и журнал healer с оптимистической блокировкой Kubernetes."""

import copy
import json
import math
import os
import time
import uuid
from datetime import datetime, timezone

from kubernetes import client, config
from kubernetes.client.rest import ApiException


class StateUnavailable(Exception):
    """Состояние или право записи нельзя подтвердить; вмешательство запрещено."""


def empty_document():
    return {"version": 1, "state": {}, "operations": [], "events": []}


STATE_FLAGS = ("_escalated", "_scale_down_pending", "_scaled_by_healer", "_recovery_pending",
               "_resolved_pending", "_mttr_sent", "_uncertain")
STATE_TIMES = ("_broke_at", "_fixed_at", "_applied_at")
STATE_KEYS = set(STATE_FLAGS + STATE_TIMES + ("_actions", "_acted_alert", "_cooldown_until", "_incident_name",
                 "_last_recovery", "_scale_owner", "_action_intent", "_closed", "_recovery_targets", "_pending"))
OPERATION_ACTIONS = {"scale", "scale_down", "delete_pod", "restart_redis", "rollback"}
OPERATION_STATUSES = {"prepared", "applied", "failed", "unknown", "observed_after_restart"}


def require_valid(condition):
    if not condition:
        raise StateUnavailable("Некорректное состояние healer; вмешательство запрещено")


def valid_number(value, nullable=False):
    try:
        return nullable and value is None or type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def valid_text(value, nullable=False):
    return nullable and value is None or isinstance(value, str) and 0 < len(value) <= 20_000


def validate_identity(identity):
    require_valid(isinstance(identity, dict) and set(identity) == {"uid", "generation", "revision"})
    require_valid(valid_text(identity["uid"]) and type(identity["generation"]) is int and identity["generation"] >= 0
                  and valid_text(identity["revision"], nullable=True))


def validate_operation(operation):
    require_valid(isinstance(operation, dict)
                  and {"id", "action", "target", "before", "prepared_at", "status"} <= set(operation))
    require_valid(valid_text(operation["id"]) and valid_text(operation["target"])
                  and isinstance(operation["action"], str) and isinstance(operation["status"], str)
                  and operation["action"] in OPERATION_ACTIONS and operation["status"] in OPERATION_STATUSES
                  and valid_number(operation["prepared_at"]))
    require_valid(isinstance(operation["before"], dict) and valid_text(operation["before"].get("uid")))
    if operation.get("owner_after") is not None:
        validate_identity(operation["owner_after"])
    require_valid(valid_number(operation.get("api_accepted_at"), nullable=True))


def validate_state(snapshot, allow_empty=True):
    """Полный снимок проверяется до изменения состояния работающего автомата."""
    require_valid(isinstance(snapshot, dict))
    if not snapshot and allow_empty:
        return
    require_valid(set(snapshot) == STATE_KEYS)
    require_valid(type(snapshot["_actions"]) is int and snapshot["_actions"] >= 0)
    require_valid(all(type(snapshot[key]) is bool for key in STATE_FLAGS))
    require_valid(valid_number(snapshot["_cooldown_until"]))
    require_valid(all(valid_number(snapshot[key], nullable=True) for key in STATE_TIMES))
    require_valid(all(valid_text(snapshot[key], nullable=True) for key in ("_acted_alert", "_incident_name")))
    for key in ("_closed", "_recovery_targets"):
        require_valid(isinstance(snapshot[key], list) and all(valid_text(value) for value in snapshot[key]))
    require_valid(set(snapshot["_recovery_targets"]) <= {"checkout", "redis"})
    if snapshot["_scale_owner"] is not None:
        validate_identity(snapshot["_scale_owner"])
    if snapshot["_action_intent"] is not None:
        validate_operation(snapshot["_action_intent"])
    history = snapshot["_last_recovery"]
    if history is not None:
        keys = {"incident", "started_at", "api_accepted_at", "slo_restored_at", "resolved_received_at"}
        require_valid(isinstance(history, dict) and set(history) == keys and valid_text(history["incident"], nullable=True))
        require_valid(all(valid_number(history[key], nullable=True) for key in keys - {"incident"}))
    pending = snapshot["_pending"]
    if pending is not None:
        keys = {"diagnosis", "mode", "action", "target", "detail", "deployment_identity", "approval_id", "approval_expires_at"}
        require_valid(isinstance(pending, dict) and keys <= set(pending) <= keys | {"cart_seq"})
        require_valid(all(valid_text(pending[key]) for key in ("diagnosis", "mode", "action", "target"))
                      and pending["mode"] == "approval" and pending["diagnosis"] in {"redis", "bad_release"}
                      and pending["action"] in {"restart_redis", "rollback"}
                      and pending["target"] == {"restart_redis": "redis", "rollback": "checkout"}[pending["action"]]
                      and valid_text(pending["detail"]) and valid_text(pending["approval_id"])
                      and valid_number(pending["approval_expires_at"]))
        validate_identity(pending["deployment_identity"])
        require_valid(pending.get("cart_seq") is None or type(pending["cart_seq"]) is int and pending["cart_seq"] >= 0)


def validate_document(document):
    require_valid(isinstance(document, dict) and set(document) == {"version", "state", "operations", "events"})
    require_valid(type(document["version"]) is int and document["version"] == 1)
    validate_state(document["state"])
    require_valid(isinstance(document["operations"], list) and len(document["operations"]) <= 100
                  and isinstance(document["events"], list) and len(document["events"]) <= 200)
    for operation in document["operations"]:
        validate_operation(operation)
    for event in document["events"]:
        require_valid(isinstance(event, dict) and valid_text(event.get("event")) and valid_number(event.get("time")))
    try:
        json.dumps(document, allow_nan=False)
    except (ValueError, TypeError):
        raise StateUnavailable("Журнал healer содержит недопустимые значения") from None


class MemoryStore:
    """Изолированное хранилище для управляемых тестов вне кластера."""

    durable = False
    leader = True
    acquired_new = False

    def __init__(self):
        self.document = empty_document()

    def acquire(self):
        return True

    def assert_leader(self):
        return None

    def authorize_write(self):
        if not self.acquire():
            raise StateUnavailable("Lease healer не обновлен перед записью")
        self.assert_leader()

    def load(self):
        return copy.deepcopy(self.document["state"])

    def peek(self):
        return copy.deepcopy(self.document)

    def save(self, state):
        self.document["state"] = copy.deepcopy(state)


class KubernetesStore(MemoryStore):
    durable = True

    def __init__(self, core=None, coordination=None, clock=time.time, owner=None):
        if core is None:
            config.load_incluster_config()
        super().__init__()
        self.core = core or client.CoreV1Api()
        self.coordination = coordination or client.CoordinationV1Api()
        self.clock = clock
        self.owner = owner or f'{os.getenv("POD_UID", "healer")}:{uuid.uuid4().hex}'
        self.namespace = os.getenv("HEALER_STATE_NAMESPACE", "heal")
        self.name = "healer-state"
        self.lease_name = "healer-leader"
        self.duration = 30
        self.resource_version = None
        self.original = None
        self.initialized = False
        self.leader = False

    def _lease(self):
        return self.coordination.read_namespaced_lease(self.lease_name, self.namespace, _request_timeout=5)

    def _current_holder(self, lease):
        spec = lease.spec
        if not spec or not spec.holder_identity or not spec.renew_time:
            return None
        expiry = spec.renew_time.timestamp() + (spec.lease_duration_seconds or self.duration)
        return spec.holder_identity if self.clock() < expiry else None

    def acquire(self):
        try:
            lease = self._lease()
            holder = self._current_holder(lease)
            if holder not in (None, self.owner):
                self.leader = False
                return False
            self.acquired_new = holder != self.owner
            now = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()
            self.coordination.patch_namespaced_lease(self.lease_name, self.namespace, {
                "metadata": {"resourceVersion": lease.metadata.resource_version},
                "spec": {"holderIdentity": self.owner, "leaseDurationSeconds": self.duration, "renewTime": now},
            }, _request_timeout=5)
            self.leader = True
            return True
        except ApiException as exc:
            self.leader = False
            if exc.status == 409:
                return False
            raise StateUnavailable("Не удалось подтвердить Lease healer") from None
        except Exception:
            self.leader = False
            raise StateUnavailable("Не удалось подтвердить Lease healer") from None

    def assert_leader(self):
        try:
            if self._current_holder(self._lease()) != self.owner:
                self.leader = False
                raise StateUnavailable("Lease healer принадлежит другому процессу или истек")
        except StateUnavailable:
            raise
        except Exception:
            self.leader = False
            raise StateUnavailable("Право записи healer не подтверждено") from None

    def _read(self):
        try:
            obj = self.core.read_namespaced_config_map(self.name, self.namespace, _request_timeout=5)
            initialized = (getattr(obj.metadata, "annotations", None) or {}).get("mtc-sre/state-initialized") == "true"
            data = obj.data or {}
            if "state.json" not in data:
                require_valid(not initialized)
                document = empty_document()
            else:
                document = json.loads(data["state.json"])
                validate_document(document)
                require_valid(not initialized or bool(document["state"]))
            return document, obj.metadata.resource_version, initialized
        except Exception:
            raise StateUnavailable("Состояние healer не удалось прочитать; автомат запрещен") from None

    def peek(self):
        return self._read()[0]

    def load(self):
        self.assert_leader()
        self.document, self.resource_version, self.initialized = self._read()
        self.original = json.dumps(self.document, ensure_ascii=False, sort_keys=True)
        return copy.deepcopy(self.document["state"])

    def save(self, state):
        self.assert_leader()
        candidate = copy.deepcopy(self.document)
        candidate["state"] = copy.deepcopy(state)
        candidate["operations"] = candidate["operations"][-100:]
        candidate["events"] = candidate["events"][-200:]
        validate_state(state, allow_empty=False)
        validate_document(candidate)
        encoded = json.dumps(candidate, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if encoded == self.original and self.initialized:
            return
        if len(encoded.encode("utf-8")) > 500_000:
            raise StateUnavailable("Журнал healer превышает допустимый размер")
        try:
            obj = self.core.patch_namespaced_config_map(self.name, self.namespace, {
                "metadata": {"resourceVersion": self.resource_version,
                             "annotations": {"mtc-sre/state-initialized": "true"}}, "data": {"state.json": encoded},
            }, _request_timeout=5)
            self.resource_version = obj.metadata.resource_version
            self.original = encoded
            self.document = candidate
            self.initialized = True
        except Exception:
            raise StateUnavailable("Состояние healer не сохранено; вмешательство запрещено") from None


def create_store():
    mode = os.getenv("HEALER_STATE_BACKEND", "kubernetes" if os.getenv("KUBERNETES_SERVICE_HOST") else "memory")
    if mode == "kubernetes":
        return KubernetesStore()
    if mode == "memory":
        return MemoryStore()
    raise StateUnavailable("Неизвестный способ хранения состояния healer")
