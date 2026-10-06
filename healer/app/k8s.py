import datetime

from kubernetes import client, config
from kubernetes.client.rest import ApiException


class ActionSkipped(Exception):
    """Действие неприменимо к текущему состоянию кластера."""


class K8sActuator:
    def __init__(self):
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self.apps = client.AppsV1Api()
        self.core = client.CoreV1Api()
        self.batch = client.BatchV1Api()

    def _write_guard(self, dry_run):
        guard = getattr(self, "before_write", None)
        if guard and not dry_run:
            guard()

    def scale(self, namespace: str, name: str, replicas: int, dry_run: bool = False, expected_replicas=None,
              expected_identity=None, operation_id=None) -> str:
        kwargs = {"dry_run": "All"} if dry_run else {}
        deployment = self.apps.read_namespaced_deployment(name, namespace, _request_timeout=5)
        current = int(deployment.spec.replicas or 0)
        if expected_replicas is not None and current != expected_replicas:
            raise ActionSkipped("Число реплик изменено после измерения")
        if replicas < current and expected_replicas is None:
            raise ActionSkipped("Уменьшение чужого масштабирования запрещено")
        if current == replicas:
            raise ActionSkipped("Требуемое число реплик уже установлено")
        self._guarded_metadata(deployment, expected_identity)
        metadata = {"uid": deployment.metadata.uid, "resourceVersion": deployment.metadata.resource_version}
        if operation_id:
            metadata["annotations"] = {"mtc-sre/healer-operation": operation_id}
        self._write_guard(dry_run)
        self.apps.patch_namespaced_deployment(
            name,
            namespace,
            {"metadata": metadata,
             "spec": {"replicas": replicas}},
            **kwargs,
            _request_timeout=5,
        )
        prefix = "dry-run " if dry_run else ""
        return f"{prefix}scale replicas={replicas}"

    def pod_uid(self, namespace, name):
        return self.core.read_namespaced_pod(name, namespace, _request_timeout=5).metadata.uid

    def delete_pod(self, namespace: str, name: str, dry_run: bool = False, expected_uid=None, operation_id=None) -> str:
        kwargs = {"dry_run": "All"} if dry_run else {}
        uid = self.pod_uid(namespace, name)
        if expected_uid is not None and uid != expected_uid:
            raise ActionSkipped("Под заменен после диагностики")
        self._write_guard(dry_run)
        self.core.delete_namespaced_pod(name, namespace,
                                       body=client.V1DeleteOptions(preconditions=client.V1Preconditions(uid=uid)),
                                       _request_timeout=5, **kwargs)
        prefix = "dry-run " if dry_run else ""
        return f"{prefix}delete pod {name}"

    def replicas(self, namespace: str, name: str) -> int:
        deployment = self.apps.read_namespaced_deployment(name, namespace, _request_timeout=5)
        return int(deployment.spec.replicas or 0)

    @staticmethod
    def _identity(deployment):
        metadata = deployment.metadata
        return {
            "uid": metadata.uid,
            "generation": metadata.generation,
            "revision": (metadata.annotations or {}).get("deployment.kubernetes.io/revision"),
        }

    def deployment_identity(self, namespace: str, name: str):
        return self._identity(self.apps.read_namespaced_deployment(name, namespace, _request_timeout=5))

    def _guarded_metadata(self, deployment, expected_identity):
        if expected_identity is None:
            return {}
        if self._identity(deployment) != expected_identity:
            raise ActionSkipped("Deployment изменился после запроса подтверждения")
        return {
            "uid": deployment.metadata.uid,
            "resourceVersion": deployment.metadata.resource_version,
        }

    def deployment_ready(self, namespace: str, name: str) -> bool:
        deployment = self.apps.read_namespaced_deployment(name, namespace, _request_timeout=5)
        desired = int(deployment.spec.replicas or 0)
        status = deployment.status
        if desired <= 0 or status is None:
            return False
        return (
            (status.observed_generation or 0) >= (deployment.metadata.generation or 0)
            and (status.updated_replicas or 0) == desired
            and (status.available_replicas or 0) >= desired
            and (status.replicas or 0) == desired
        )

    def ready(self, namespace: str, app: str) -> bool:
        listed = self.core.list_namespaced_pod(namespace, label_selector=f"app={app}", _request_timeout=5)
        for pod in listed.items:
            conditions = pod.status.conditions or []
            if any(item.type == "Ready" and item.status == "True" for item in conditions):
                return True
        return False

    def telemetry_pods(self, namespace: str, name: str):
        """Рабочие поды текущей ревизии, принадлежащие указанному Deployment."""
        deployment = self.apps.read_namespaced_deployment(name, namespace, _request_timeout=5)
        revision = (deployment.metadata.annotations or {}).get("deployment.kubernetes.io/revision")
        owners = set()
        for replica_set in self.apps.list_namespaced_replica_set(namespace, _request_timeout=5).items:
            if ((replica_set.metadata.annotations or {}).get("deployment.kubernetes.io/revision") == revision
                and any(owner.kind == "Deployment" and owner.uid == deployment.metadata.uid
                        for owner in replica_set.metadata.owner_references or [])):
                owners.add(replica_set.metadata.uid)
        selected = self.core.list_namespaced_pod(namespace, label_selector=f"app={name}", _request_timeout=5)
        return {
            pod.metadata.name: pod.metadata.uid for pod in selected.items
            if not pod.metadata.deletion_timestamp and pod.status.phase == "Running"
            and any(owner.kind == "ReplicaSet" and owner.uid in owners
                    for owner in pod.metadata.owner_references or [])
        }

    def revision_age_seconds(self, namespace: str, name: str):
        deployment = self.apps.read_namespaced_deployment(name, namespace, _request_timeout=5)
        revision = (deployment.metadata.annotations or {}).get("deployment.kubernetes.io/revision")
        if not deployment.metadata.uid or revision is None:
            return None
        newest = None
        listed = self.apps.list_namespaced_replica_set(namespace, _request_timeout=5)
        for replica_set in listed.items:
            owners = replica_set.metadata.owner_references or []
            owned = any(owner.kind == "Deployment" and owner.uid == deployment.metadata.uid for owner in owners)
            if not owned or replica_set.metadata.creation_timestamp is None:
                continue
            rev = (replica_set.metadata.annotations or {}).get("deployment.kubernetes.io/revision")
            if rev == revision and (newest is None or replica_set.metadata.creation_timestamp > newest):
                newest = replica_set.metadata.creation_timestamp
        if newest is None:
            return None
        now = datetime.datetime.now(datetime.timezone.utc)
        return (now - newest).total_seconds()

    def spike_running(self, namespace: str) -> bool:
        try:
            job = self.batch.read_namespaced_job("k6-spike", namespace, _request_timeout=5)
        except ApiException as exc:
            if exc.status == 404:
                return False
            raise
        status = job.status
        if status is None:
            return True
        if status.succeeded or status.failed:
            return False
        return True

    def restart(self, namespace: str, name: str, dry_run: bool = False, expected_identity=None, operation_id=None) -> str:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        kwargs = {"dry_run": "All"} if dry_run else {}
        metadata = {}
        if expected_identity is not None:
            deployment = self.apps.read_namespaced_deployment(name, namespace, _request_timeout=5)
            metadata = self._guarded_metadata(deployment, expected_identity)
        if operation_id:
            metadata["annotations"] = {"mtc-sre/healer-operation": operation_id}
        self._write_guard(dry_run)
        self.apps.patch_namespaced_deployment(
            name,
            namespace,
            {
                "metadata": metadata,
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {"kubectl.kubernetes.io/restartedAt": now}
                        }
                    }
                }
            },
            **kwargs,
            _request_timeout=5,
        )
        prefix = "dry-run " if dry_run else ""
        return f"{prefix}restart {name}"

    def rollback(self, namespace: str, name: str, dry_run: bool = False, expected_identity=None, operation_id=None) -> str:
        deployment = self.apps.read_namespaced_deployment(name, namespace, _request_timeout=5)
        metadata = self._guarded_metadata(deployment, expected_identity)
        current = int((deployment.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "0"))
        if current <= 1:
            raise ActionSkipped("Нет предыдущей ревизии для отката")
        previous = self._previous_replicaset(namespace, name, current - 1, deployment.metadata.uid)
        if previous is None:
            raise ActionSkipped("ReplicaSet предыдущей ревизии не найден")
        template = client.ApiClient().sanitize_for_serialization(previous.spec.template)
        template.get("metadata", {}).get("labels", {}).pop("pod-template-hash", None)
        if operation_id:
            metadata["annotations"] = {"mtc-sre/healer-operation": operation_id}
        kwargs = {"dry_run": "All"} if dry_run else {}
        self._write_guard(dry_run)
        self.apps.patch_namespaced_deployment(
            name, namespace, {"metadata": metadata, "spec": {"template": template}}, _request_timeout=5, **kwargs,
        )
        prefix = "dry-run " if dry_run else ""
        return f"{prefix}rollback to revision {current - 1}"

    def operation_observed(self, namespace, operation):
        if operation["action"] == "delete_pod":
            try:
                return self.pod_uid(namespace, operation["target"]) != operation["before"]["uid"]
            except ApiException as exc:
                if exc.status == 404:
                    return True
                raise
        name = "redis" if operation["action"] == "restart_redis" else "checkout"
        deployment = self.apps.read_namespaced_deployment(name, namespace, _request_timeout=5)
        return ((deployment.metadata.annotations or {}).get("mtc-sre/healer-operation") == operation["id"]
                and deployment.metadata.uid == operation["before"]["uid"])

    def _previous_replicaset(self, namespace: str, name: str, revision: int, deployment_uid=None):
        if deployment_uid is None:
            deployment_uid = self.apps.read_namespaced_deployment(name, namespace, _request_timeout=5).metadata.uid
        if not deployment_uid:
            return None
        listed = self.apps.list_namespaced_replica_set(namespace, _request_timeout=5)
        for replica_set in listed.items:
            owners = replica_set.metadata.owner_references or []
            owned = any(owner.kind == "Deployment" and owner.uid == deployment_uid for owner in owners)
            if not owned:
                continue
            rev = int((replica_set.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "0"))
            if rev == revision:
                return replica_set
        return None
