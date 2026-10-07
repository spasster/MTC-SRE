"""Конфликт статуса переносится, изменение оператора останавливает проверку."""

import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from kubernetes.client.rest import ApiException

from app.k8s import K8sActuator

spec = importlib.util.spec_from_file_location("scale_verifier", Path(__file__).resolve().parents[2] / "scripts/verify-scale.py")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


class Backend:
    def __init__(self, cleanup_conflicts=0, operator_change=False, unknown_write=False):
        self.deployment = SimpleNamespace(metadata=SimpleNamespace(uid="checkout-current", generation=3,
            resource_version="1", annotations={"deployment.kubernetes.io/revision": "1"}), spec=SimpleNamespace(replicas=2))
        self.conflicts, self.operator_change, self.unknown_write = cleanup_conflicts, operator_change, unknown_write
        self.actor = object.__new__(K8sActuator)
        self.actor.apps = Mock()
        self.actor.apps.read_namespaced_deployment.side_effect = lambda *args, **kwargs: copy.deepcopy(self.deployment)
        self.actor.apps.patch_namespaced_deployment.side_effect = self.patch

    def patch(self, name, namespace, body, **kwargs):
        if kwargs.get("dry_run"):
            return copy.deepcopy(self.deployment)
        if body["spec"]["replicas"] == 2 and self.conflicts:
            self.conflicts -= 1
            self.deployment.metadata.resource_version = str(int(self.deployment.metadata.resource_version) + 1)
            if self.operator_change:
                self.deployment.spec.replicas = 7
                self.deployment.metadata.generation += 1
            raise ApiException(status=409)
        self.deployment.spec.replicas = body["spec"]["replicas"]
        self.deployment.metadata.generation += 1
        if self.unknown_write:
            raise TimeoutError("Результат записи не получен")
        return copy.deepcopy(self.deployment)


def test_status_conflict_is_retried_without_disabling_guards(monkeypatch):
    monkeypatch.setattr(verifier.time, "sleep", lambda seconds: None)
    backend = Backend(cleanup_conflicts=1)
    result = verifier.verify(backend.actor)
    assert result["passed"] and result["conflicts"]["cleanup"] == 1
    assert backend.deployment.spec.replicas == 2
    writes = backend.actor.apps.patch_namespaced_deployment.call_args_list
    assert all(call.args[2]["metadata"]["uid"] == "checkout-current" for call in writes)
    assert writes[-2].args[2]["metadata"]["resourceVersion"] != writes[-1].args[2]["metadata"]["resourceVersion"]


def test_operator_change_after_conflict_is_preserved(monkeypatch):
    monkeypatch.setattr(verifier.time, "sleep", lambda seconds: None)
    backend = Backend(cleanup_conflicts=1, operator_change=True)
    result = verifier.verify(backend.actor)
    assert not result["passed"] and result["cleanup_errors"]
    assert backend.deployment.spec.replicas == 7
    assert backend.actor.apps.patch_namespaced_deployment.call_count == 3


def test_exhausted_cleanup_still_emits_a_failed_json(monkeypatch, capsys):
    monkeypatch.setattr(verifier.time, "sleep", lambda seconds: None)
    backend = Backend(cleanup_conflicts=3)
    with pytest.raises(SystemExit):
        verifier.main(backend.actor)
    result = json.loads(capsys.readouterr().out)
    assert not result["passed"] and not result["original_replicas_restored"]
    assert result["cleanup_errors"] and backend.deployment.spec.replicas == 5


def test_unknown_write_is_not_repeated_or_guessed(monkeypatch):
    monkeypatch.setattr(verifier.time, "sleep", lambda seconds: None)
    backend = Backend(unknown_write=True)
    result = verifier.verify(backend.actor)
    assert not result["passed"] and result["errors"][0]["error_type"] == "TimeoutError"
    assert backend.actor.apps.patch_namespaced_deployment.call_count == 2
