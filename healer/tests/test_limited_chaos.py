"""Удержание SLO проверяется независимо от числа готовых резервных подов."""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from kubernetes.client.rest import ApiException

from app.k8s import K8sActuator
from app import observability as obs
from prometheus_client import generate_latest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
spec = importlib.util.spec_from_file_location("limited_chaos", Path(__file__).resolve().parents[2] / "scripts/verify-limited-chaos.py")
limited = importlib.util.module_from_spec(spec)
spec.loader.exec_module(limited)


def test_reserve_replica_loss_does_not_replace_service_sli():
    sample = {"p95": .2, "error_ratio": 0, "request_rate": 2, "synthetic_success": 1,
              "synthetic_time": 100, "time": 110, "rollout_ready": False}
    assert limited.slo_held(sample)
    assert not limited.slo_held({**sample, "p95": .301})
    assert not limited.slo_held({**sample, "synthetic_time": 50})
    assert not limited.slo_held({**sample, "error_ratio": None})


def test_client_loss_and_invalid_body_cannot_pass():
    client = {"http_reqs": {"count": 300}, "dropped_iterations": {"count": 0},
              "http_req_failed": {"rate": 0}, "checks": {"rate": 1}, "http_req_duration": {"p(95)": 200}}
    assert limited.client_passed(client)
    assert not limited.client_passed({**client, "dropped_iterations": {"count": 1}})
    assert not limited.client_passed({**client, "checks": {"rate": .99}})
    assert not limited.client_passed({**client, "http_req_duration": {"p(95)": 300}})


def test_observer_requires_actual_injection_and_limits_api_reads():
    actor = object.__new__(K8sActuator)
    actor.custom = Mock()
    actor.custom.get_namespaced_custom_object.side_effect = [
        {"status": {"conditions": [{"type": "Selected", "status": "True"}]}},
        {"status": {"conditions": [{"type": "AllInjected", "status": "True"}]}},
        ApiException(status=404),
    ]
    assert actor.limited_chaos("demo") == {"pod": False, "network": True, "cpu": False}
    assert all(call.kwargs["_request_timeout"] == 5 for call in actor.custom.get_namespaced_custom_object.call_args_list)
    obs.chaos(None)
    assert 'healer_chaos_injection_observed{fault="cpu"} NaN' in generate_latest(obs.REGISTRY).decode()


def test_chaos_api_failure_is_not_a_zero_measurement():
    actor = object.__new__(K8sActuator)
    actor.custom = Mock()
    actor.custom.get_namespaced_custom_object.side_effect = ApiException(status=403)
    with pytest.raises(ApiException):
        actor.limited_chaos("demo")
