"""Удержание SLO проверяется независимо от числа готовых резервных подов."""

import importlib.util
import copy
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from kubernetes.client.rest import ApiException

from app.k8s import K8sActuator
from app import observability as obs
from prometheus_client import generate_latest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
spec = importlib.util.spec_from_file_location("limited_chaos", Path(__file__).resolve().parents[2] / "scripts/verify-limited-chaos.py")
limited = importlib.util.module_from_spec(spec)
spec.loader.exec_module(limited)
import experiment


def test_reserve_replica_loss_does_not_replace_service_sli():
    sample = {"p95": .2, "error_ratio": 0, "request_rate": 2, "synthetic_success": 1,
              "synthetic_time": 100, "time": 110, "rollout_ready": False}
    assert limited.slo_held(sample)
    assert not limited.slo_held({**sample, "p95": .301})
    assert not limited.slo_held({**sample, "synthetic_time": 50})
    assert not limited.slo_held({**sample, "error_ratio": None})


def test_client_loss_and_invalid_body_cannot_pass():
    client = {"http_reqs": {"count": 360}, "dropped_iterations": {"count": 0},
              "http_req_failed": {"rate": 0}, "checks": {"rate": 1}, "http_req_duration": {"p(95)": 200}}
    assert limited.client_passed(client)
    assert not limited.client_passed({**client, "dropped_iterations": {"count": 1}})
    assert not limited.client_passed({**client, "checks": {"rate": .99}})
    assert not limited.client_passed({**client, "http_req_duration": {"p(95)": 300}})


def test_complete_response_budget_matches_the_declared_availability_slo():
    client = {"http_reqs": {"count": 360}, "dropped_iterations": {"count": 0},
              "http_req_failed": {"rate": 1 / 360}, "checks": {"rate": 359 / 360},
              "http_req_duration": {"p(95)": 100}}
    assert limited.client_passed(client)
    assert not limited.client_passed({**client, "checks": {"rate": 358 / 360}})
    assert not limited.client_passed({**client, "http_req_failed": {"rate": 2 / 360}})


def test_delay_or_total_loss_cannot_prove_a_bounded_partition():
    reachable = {"reachable": True}
    cut = {"reachable": False}
    before = {"blocked": reachable, "alternate": reachable}
    assert limited.network_cut_proven({"paths_before": before, "paths_during": {"blocked": cut, "alternate": reachable}})
    assert not limited.network_cut_proven({"paths_before": before, "paths_during": before})
    assert not limited.network_cut_proven({"paths_before": before, "paths_during": {"blocked": cut, "alternate": cut}})


def test_payment_retry_cannot_repeat_a_processed_operation():
    path = Path(__file__).resolve().parents[2] / "k8s/resilience.yaml"
    policies = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    route = next(policy for policy in policies if policy["kind"] == "VirtualService" and policy["metadata"]["name"] == "payment")["spec"]["http"][0]
    assert route["match"] == [{"method": {"exact": "POST"}, "uri": {"exact": "/pay"}}]
    assert set(route["retries"]["retryOn"].split(",")) == {"connect-failure", "refused-stream"}
    with pytest.raises(ValueError):
        limited.fault_manifest("network", "checkout-only")


def test_unused_partition_cannot_prove_application_failover():
    before = {"blocked": {"failed_outlier_check": False, "connect_failures": 0, "requests_succeeded": 10},
              "alternate": {"failed_outlier_check": False, "connect_failures": 0, "requests_succeeded": 10}}
    after = {"blocked": {"failed_outlier_check": True, "connect_failures": 1, "requests_succeeded": 10},
             "alternate": {"failed_outlier_check": False, "connect_failures": 0, "requests_succeeded": 11}}
    case = {"payment_targets": {"blocked": "blocked", "alternate": "alternate"},
            "mesh_before": {"hosts": before}, "mesh_during": {"hosts": after}}
    assert limited.network_mesh_proven(case)
    assert not limited.network_mesh_proven({**case, "mesh_during": {"hosts": before}})
    after["alternate"]["requests_succeeded"] = 10
    assert not limited.network_mesh_proven(case)


@pytest.mark.parametrize("field,value", [("uid", "operator-object"), ("generation", 9), ("replicas", 4)])
def test_own_test_scale_cannot_overwrite_an_operator_change(monkeypatch, field, value):
    expected = {"uid": "checkout-current", "generation": 5, "replicas": 3}
    current = {**expected, field: value}
    original = {"metadata": {"uid": expected["uid"], "generation": expected["generation"], "resourceVersion": "7",
                             "annotations": {experiment.OWNER_ANNOTATION: "owned"}}, "spec": {"replicas": expected["replicas"]}}
    obj = copy.deepcopy(original)
    obj["metadata"].update(uid=current["uid"], generation=current["generation"])
    obj["spec"]["replicas"] = current["replicas"]
    owner = limited.OwnedDeployment("checkout", "demo", original, "owned")
    owner.changed = True
    monkeypatch.setattr(experiment, "deployment", lambda *args: obj)
    write = Mock()
    monkeypatch.setattr(experiment, "run_command", write)
    with pytest.raises(RuntimeError):
        owner.cleanup()
    write.assert_not_called()


@pytest.mark.parametrize("gate_failed", [True, False])
def test_limited_cleanup_preserves_operator_background_after_rejection_or_unknown_write(monkeypatch, tmp_path, gate_failed):
    objects = {name: {"metadata": {"uid": "original-" + name, "generation": 1, "resourceVersion": "10", "annotations": {}},
                      "spec": {"replicas": 1 if name in ("healer", "k6-background") else 2,
                               "template": {"spec": {"containers": [{"resources": {}}]}}}}
               for name in ("checkout", "payment", "healer", "k6-background")}
    target, writes, commands = tmp_path / "result.json", [], []
    monkeypatch.setattr(sys, "argv", ["limited", "--output", str(target)])
    read = lambda name, namespace: copy.deepcopy(objects[name])
    monkeypatch.setattr(limited, "deployment", read)
    monkeypatch.setattr(experiment, "deployment", read)
    monkeypatch.setattr(limited, "cmd", lambda *args, **kwargs: commands.append(args) or json.dumps({"items": []}))
    monkeypatch.setattr(limited, "wait_healthy", lambda *args, **kwargs: None)
    def stop_before_mutation():
        objects["k6-background"]["spec"]["replicas"] = 7
        raise RuntimeError("Допуск не получен")
    monkeypatch.setattr(limited, "wait_incident_closed", stop_before_mutation if gate_failed else lambda: {"incident_closed": True})
    def lost_response(*args, **kwargs):
        writes.append(args)
        objects["k6-background"]["spec"]["replicas"] = 7
        raise RuntimeError("Ответ первой записи потерян")
    monkeypatch.setattr(experiment, "run_command", lost_response)

    with pytest.raises(SystemExit):
        limited.main()
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["passed"] is False and objects["k6-background"]["spec"]["replicas"] == 7
    assert len(writes) == (0 if gate_failed else 1)
    assert not any("delete" in args or "scale" in args for args in commands)
    assert len(saved["cleanup_errors"]) == (0 if gate_failed else 1)


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
