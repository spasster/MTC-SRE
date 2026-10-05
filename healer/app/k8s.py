import datetime

from kubernetes import client, config
from kubernetes.client.rest import ApiException


class K8sActuator:
    def __init__(self):
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self.apps = client.AppsV1Api()
        self.core = client.CoreV1Api()
        self.batch = client.BatchV1Api()

    def scale(self, namespace: str, name: str, replicas: int, dry_run: bool = False) -> str:
        kwargs = {"dry_run": "All"} if dry_run else {}
        self.apps.patch_namespaced_deployment(
            name,
            namespace,
            {"spec": {"replicas": replicas}},
            **kwargs,
        )
        prefix = "dry-run " if dry_run else ""
        return f"{prefix}scale replicas={replicas}"

    def delete_pod(self, namespace: str, name: str, dry_run: bool = False) -> str:
        kwargs = {"dry_run": "All"} if dry_run else {}
        self.core.delete_namespaced_pod(name, namespace, **kwargs)
        prefix = "dry-run " if dry_run else ""
        return f"{prefix}delete pod {name}"

    def replicas(self, namespace: str, name: str) -> int:
        deployment = self.apps.read_namespaced_deployment(name, namespace)
        return int(deployment.spec.replicas or 0)

    def ready(self, namespace: str, app: str) -> bool:
        listed = self.core.list_namespaced_pod(namespace, label_selector=f"app={app}")
        for pod in listed.items:
            conditions = pod.status.conditions or []
            if any(item.type == "Ready" and item.status == "True" for item in conditions):
                return True
        return False

    def revision_age_seconds(self, namespace: str, name: str):
        newest = None
        newest_rev = -1
        listed = self.apps.list_namespaced_replica_set(namespace)
        for replica_set in listed.items:
            owners = replica_set.metadata.owner_references or []
            owned = any(owner.kind == "Deployment" and owner.name == name for owner in owners)
            if not owned or replica_set.metadata.creation_timestamp is None:
                continue
            rev = int((replica_set.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "0"))
            if rev >= newest_rev:
                newest_rev = rev
                newest = replica_set.metadata.creation_timestamp
        if newest is None:
            return None
        now = datetime.datetime.now(datetime.timezone.utc)
        return (now - newest).total_seconds()

    def spike_running(self, namespace: str) -> bool:
        try:
            job = self.batch.read_namespaced_job("k6-spike", namespace)
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

    def restart(self, namespace: str, name: str, dry_run: bool = False) -> str:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        kwargs = {"dry_run": "All"} if dry_run else {}
        self.apps.patch_namespaced_deployment(
            name,
            namespace,
            {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {"kubectl.kubernetes.io/restartedAt": now}
                        }
                    }
                }
            },
            **kwargs,
        )
        prefix = "dry-run " if dry_run else ""
        return f"{prefix}restart {name}"

    def rollback(self, namespace: str, name: str, dry_run: bool = False) -> str:
        deployment = self.apps.read_namespaced_deployment(name, namespace)
        current = int((deployment.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "0"))
        if current <= 1:
            return "rollback skipped: no previous revision"
        previous = self._previous_replicaset(namespace, name, current - 1)
        if previous is None:
            return "rollback skipped: previous replicaset not found"
        template = client.ApiClient().sanitize_for_serialization(previous.spec.template)
        kwargs = {"dry_run": "All"} if dry_run else {}
        self.apps.patch_namespaced_deployment(name, namespace, {"spec": {"template": template}}, **kwargs)
        prefix = "dry-run " if dry_run else ""
        return f"{prefix}rollback to revision {current - 1}"

    def _previous_replicaset(self, namespace: str, name: str, revision: int):
        listed = self.apps.list_namespaced_replica_set(namespace)
        for replica_set in listed.items:
            owners = replica_set.metadata.owner_references or []
            owned = any(owner.kind == "Deployment" and owner.name == name for owner in owners)
            if not owned:
                continue
            rev = int((replica_set.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "0"))
            if rev == revision:
                return replica_set
        return None
