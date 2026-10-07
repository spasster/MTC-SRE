"""Выбор CPU-нагрузки не подменяет отрицательные или несовместимые замеры."""
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from cpu_profile import selected_profile


def measured():
    sample = {"p95": .15, "error_ratio": 0, "request_rate": 9, "cpu_throttle": .2,
              "synthetic_success": 1, "synthetic_time": 100, "time": 110, "rollout_ready": True}
    failed = {"replicas": 2, "planned_rps": 9, "passed": False, "client_p95_ms": 550,
              "samples": [{**sample, "p95": .55, "cpu_throttle": .7} for _ in range(3)]}
    healthy = {"replicas": 5, "planned_rps": 9, "passed": True, "client_p95_ms": 150,
               "sent": 810, "dropped": 0, "client": {"http_req_failed": {"rate": 0}},
               "samples": [copy.deepcopy(sample) for _ in range(3)]}
    return {"passed": True, "cleanup_errors": [], "cluster": "mtc-ci", "source_commit": "current",
            "duration_seconds": 90, "finished_at": "2026-10-07T03:00:00+00:00", "stages": [failed, healthy]}


NOW = 1791342060


def test_same_measured_rate_is_selected_without_background_load():
    result = selected_profile(measured(), "mtc-ci", "current", now=NOW)
    assert result["total_rps"] == 9 and result["background_rps_during_spike"] == 0
    assert result["two_replicas_client_p95_ms"] == 550 and result["five_replicas_client_p95_ms"] == 150


@pytest.mark.parametrize("field,value", [
    ("passed", False), ("cleanup_errors", [{"operation": "job"}]), ("cluster", "different"),
    ("source_commit", "previous"), ("finished_at", None), ("finished_at", "2026-10-07T03:00:00"),
    ("duration_seconds", 30),
])
def test_incompatible_or_incomplete_measurements_are_rejected(field, value):
    report = measured()
    report[field] = value
    with pytest.raises(ValueError):
        selected_profile(report, "mtc-ci", "current", now=NOW)


@pytest.mark.parametrize("field,value", [("dropped", 1), ("sent", 600), ("client_p95_ms", 300), ("client_p95_ms", float("nan")), ("passed", False)])
def test_invalid_five_replica_result_cannot_define_the_profile(field, value):
    report = measured()
    report["stages"][1][field] = value
    with pytest.raises(ValueError):
        selected_profile(report, "mtc-ci", "current", now=NOW)


@pytest.mark.parametrize("side,field,value", [(0, "cpu_throttle", .1), (0, "p95", .2), (0, "error_ratio", .1),
                                             (1, "p95", .35), (1, "synthetic_success", 0), (1, "rollout_ready", False)])
def test_client_flags_do_not_replace_cpu_and_service_evidence(side, field, value):
    report = measured()
    for row in report["stages"][side]["samples"]:
        row[field] = value
    with pytest.raises(ValueError):
        selected_profile(report, "mtc-ci", "current", now=NOW)


@pytest.mark.parametrize("now", [NOW - 120, NOW + 1800])
def test_future_and_stale_measurements_are_rejected(now):
    with pytest.raises(ValueError):
        selected_profile(measured(), "mtc-ci", "current", now=now)
