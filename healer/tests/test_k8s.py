from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes import client

from app.k8s import ActionSkipped, K8sActuator


def actor(revision):
    instance = object.__new__(K8sActuator)
    instance.apps = Mock()
    instance.apps.read_namespaced_deployment.return_value = SimpleNamespace(
        metadata=SimpleNamespace(annotations={"deployment.kubernetes.io/revision": str(revision)},
                                 uid="checkout-uid", generation=3, resource_version="42")
    )
    return instance


def test_rollback_without_previous_revision_is_skipped():
    instance = actor(1)
    with pytest.raises(ActionSkipped):
        instance.rollback("demo", "checkout")
    instance.apps.patch_namespaced_deployment.assert_not_called()


@pytest.mark.parametrize("action", ["rollback", "restart"])
def test_changed_deployment_cannot_use_an_old_approval(action):
    instance = actor(3)
    expected = instance.deployment_identity("demo", "checkout")
    instance.apps.read_namespaced_deployment.return_value.metadata.generation = 4
    with pytest.raises(ActionSkipped, match="изменился"):
        getattr(instance, action)("demo", "checkout", expected_identity=expected)
    instance.apps.patch_namespaced_deployment.assert_not_called()


def test_restart_checks_resource_version_at_the_api():
    instance = actor(3)
    expected = instance.deployment_identity("demo", "checkout")
    instance.restart("demo", "checkout", dry_run=True, expected_identity=expected)
    patch = instance.apps.patch_namespaced_deployment.call_args
    assert patch.args[2]["metadata"] == {"uid": "checkout-uid", "resourceVersion": "42"}
    assert patch.kwargs == {"dry_run": "All", "_request_timeout": 5}


@pytest.mark.parametrize("expected", [None, 5])
def test_scale_does_not_reduce_operator_replicas(expected):
    instance = actor(3)
    instance.apps.read_namespaced_deployment.return_value.spec = SimpleNamespace(replicas=7)
    with pytest.raises(ActionSkipped):
        instance.scale("demo", "checkout", 2 if expected else 5, expected_replicas=expected)
    instance.apps.patch_namespaced_deployment.assert_not_called()


def test_scale_checks_resource_version_at_the_api():
    instance = actor(3)
    instance.apps.read_namespaced_deployment.return_value.spec = SimpleNamespace(replicas=5)
    instance.scale("demo", "checkout", 2, expected_replicas=5)
    body = instance.apps.patch_namespaced_deployment.call_args.args[2]
    assert body == {"metadata": {"uid": "checkout-uid", "resourceVersion": "42"}, "spec": {"replicas": 2}}


def test_rollback_without_previous_replicaset_is_skipped():
    instance = actor(3)
    instance.apps.list_namespaced_replica_set.return_value = SimpleNamespace(items=[])
    with pytest.raises(ActionSkipped):
        instance.rollback("demo", "checkout")
    instance.apps.patch_namespaced_deployment.assert_not_called()


def test_rollback_removes_controller_label_and_uses_dry_run():
    instance = actor(3)
    previous = SimpleNamespace(
        metadata=SimpleNamespace(
            owner_references=[SimpleNamespace(kind="Deployment", name="checkout", uid="checkout-uid")],
            annotations={"deployment.kubernetes.io/revision": "2"},
        ),
        spec=SimpleNamespace(template=client.V1PodTemplateSpec(
            metadata=client.V1ObjectMeta(labels={"app": "checkout", "pod-template-hash": "old"})
        )),
    )
    instance.apps.list_namespaced_replica_set.return_value = SimpleNamespace(items=[previous])
    assert instance.rollback("demo", "checkout", dry_run=True) == "dry-run rollback to revision 2"
    call = instance.apps.patch_namespaced_deployment.call_args
    assert call.kwargs == {"dry_run": "All", "_request_timeout": 5}
    assert call.args[2]["spec"]["template"]["metadata"]["labels"] == {"app": "checkout"}


def replica_set(uid, revision, age):
    return SimpleNamespace(metadata=SimpleNamespace(
        owner_references=[SimpleNamespace(kind="Deployment", name="checkout", uid=uid)],
        annotations={"deployment.kubernetes.io/revision": str(revision)},
        creation_timestamp=datetime.now(timezone.utc) - timedelta(seconds=age),
    ))


def test_orphan_replicaset_cannot_be_used_for_rollback():
    instance = actor(3)
    instance.apps.list_namespaced_replica_set.return_value = SimpleNamespace(
        items=[replica_set("deleted-deployment", 2, 10)])
    with pytest.raises(ActionSkipped, match="не найден"):
        instance.rollback("demo", "checkout", dry_run=True)
    instance.apps.patch_namespaced_deployment.assert_not_called()


def test_revision_age_uses_current_uid_and_revision():
    instance = actor(3)
    instance.apps.list_namespaced_replica_set.return_value = SimpleNamespace(items=[
        replica_set("checkout-uid", 3, 3600),
        replica_set("deleted-deployment", 99, 5),
        replica_set("checkout-uid", 2, 10),
        replica_set("checkout-uid", 4, 15),
    ])
    assert 3600 <= instance.revision_age_seconds("demo", "checkout") < 3601


def test_missing_current_replicaset_has_unknown_age():
    instance = actor(3)
    instance.apps.list_namespaced_replica_set.return_value = SimpleNamespace(
        items=[replica_set("deleted-deployment", 3, 5), replica_set("checkout-uid", 2, 10)])
    assert instance.revision_age_seconds("demo", "checkout") is None


@pytest.mark.parametrize("observed,updated,available,total,expected", [
    (3, 2, 2, 2, True),
    (2, 2, 2, 2, False),
    (3, 1, 2, 2, False),
    (3, 2, 1, 2, False),
    (3, 2, 2, 3, False),
])
def test_recovery_requires_the_current_rollout(observed, updated, available, total, expected):
    instance = actor(3)
    instance.apps.read_namespaced_deployment.return_value = SimpleNamespace(
        metadata=SimpleNamespace(generation=3),
        spec=SimpleNamespace(replicas=2),
        status=SimpleNamespace(observed_generation=observed, updated_replicas=updated, available_replicas=available, replicas=total),
    )
    assert instance.deployment_ready("demo", "checkout") is expected
