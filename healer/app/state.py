"""Состояние и журнал healer с оптимистической блокировкой Kubernetes."""

import copy
import json
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
            document = json.loads((obj.data or {}).get("state.json", "{}")) or empty_document()
            if (not isinstance(document, dict) or document.get("version") != 1
                or not isinstance(document.get("state"), dict)
                or not isinstance(document.get("operations"), list)
                or not isinstance(document.get("events"), list)
                or any(not isinstance(row, dict) for row in document["operations"] + document["events"])):
                raise ValueError("schema")
            return document, obj.metadata.resource_version
        except Exception:
            raise StateUnavailable("Состояние healer не удалось прочитать; автомат запрещен") from None

    def peek(self):
        return self._read()[0]

    def load(self):
        self.assert_leader()
        self.document, self.resource_version = self._read()
        self.original = json.dumps(self.document, ensure_ascii=False, sort_keys=True)
        return copy.deepcopy(self.document["state"])

    def save(self, state):
        self.assert_leader()
        self.document["state"] = copy.deepcopy(state)
        self.document["operations"] = self.document["operations"][-100:]
        self.document["events"] = self.document["events"][-200:]
        encoded = json.dumps(self.document, ensure_ascii=False, sort_keys=True)
        if encoded == self.original:
            return
        if len(encoded.encode("utf-8")) > 500_000:
            raise StateUnavailable("Журнал healer превышает допустимый размер")
        try:
            obj = self.core.patch_namespaced_config_map(self.name, self.namespace, {
                "metadata": {"resourceVersion": self.resource_version}, "data": {"state.json": encoded},
            }, _request_timeout=5)
            self.resource_version = obj.metadata.resource_version
            self.original = encoded
        except Exception:
            raise StateUnavailable("Состояние healer не сохранено; вмешательство запрещено") from None


def create_store():
    mode = os.getenv("HEALER_STATE_BACKEND", "kubernetes" if os.getenv("KUBERNETES_SERVICE_HOST") else "memory")
    if mode == "kubernetes":
        return KubernetesStore()
    if mode == "memory":
        return MemoryStore()
    raise StateUnavailable("Неизвестный способ хранения состояния healer")
