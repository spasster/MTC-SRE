import datetime

from kubernetes import client, config


class K8sActuator:
    def __init__(self):
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self.apps = client.AppsV1Api()

    def scale(self, namespace: str, name: str, replicas: int) -> str:
        self.apps.patch_namespaced_deployment(name, namespace, {"spec": {"replicas": replicas}})
        return f"scale replicas={replicas}"

    def restart(self, namespace: str, name: str) -> str:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
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
        )
        return "restart"

    def rollback(self, namespace: str, name: str) -> str:
        deployment = self.apps.read_namespaced_deployment(name, namespace)
        current = int((deployment.metadata.annotations or {}).get("deployment.kubernetes.io/revision", "0"))
        if current <= 1:
            return "rollback skipped: no previous revision"
        previous = self._previous_replicaset(namespace, name, current - 1)
        if previous is None:
            return "rollback skipped: previous replicaset not found"
        template = client.ApiClient().sanitize_for_serialization(previous.spec.template)
        self.apps.patch_namespaced_deployment(name, namespace, {"spec": {"template": template}})
        return f"rollback to revision {current - 1}"

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
