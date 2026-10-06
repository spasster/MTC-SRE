import pytest

from app.evidence import gather, parse_vector


class Actor:
    def revision_age_seconds(self, *args):
        return 5000

    def replicas(self, *args):
        return 5

    def ready(self, *args):
        return True


def test_empty_prometheus_results_are_not_zero_error_success():
    evidence = gather("demo", actuator=Actor(), query=lambda query: [])
    assert evidence["request_rate"] is None
    assert evidence["error_ratio"] is None
    assert evidence["p95"] is None
    assert evidence["pods"] == []


@pytest.mark.parametrize("value", ["NaN", "nan", "+Inf", "inf", "-Inf"])
def test_non_finite_samples_are_not_evidence(value):
    assert parse_vector([{"metric": {"pod": "checkout"}, "value": [0, value]}]) == []
