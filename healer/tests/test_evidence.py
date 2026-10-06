import pytest

from app.evidence import gather, parse_vector


class Actor:
    def telemetry_pods(self, *args):
        return {"checkout-a": "uid-a", "checkout-b": "uid-b"}

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


def query_complete(query):
    if query.startswith("min(probe_success"):
        return [({}, 1)]
    if query.startswith("max(time()"):
        return [({}, 5)]
    if query.startswith("histogram_quantile") and "sum by (le)" in query:
        return [({}, .05)]
    if query.startswith("up{"):
        value = 1
    elif query.startswith("max by(pod)"):
        value = 5
    elif query.startswith("histogram_quantile"):
        value = .05
    elif "container_cpu" in query:
        value = .1
    elif "http_requests_total" in query and "status=" not in query:
        value = 1.5
    else:
        value = 0
    return [({"pod": pod}, value) for pod in Actor().telemetry_pods()]


def test_complete_explicit_zero_errors_are_valid_and_scoped():
    queries = []

    def query(expression):
        queries.append(expression)
        return query_complete(expression)

    result = gather("demo", Actor(), query)
    assert result["telemetry_complete"] is True
    assert result["error_ratio"] == 0
    assert result["request_rate"] == 3
    assert {pod["uid"] for pod in result["pods"]} == {"uid-a", "uid-b"}
    assert all('namespace="demo"' in item for item in queries)
    assert all('pod=~"checkout' in item for item in queries if "probe_success" not in item)


@pytest.mark.parametrize("missing", ["status=", 'dependency="redis"', 'dependency="payment"', "application_errors_total"])
def test_missing_error_series_cannot_be_treated_as_zero(missing):
    result = gather("demo", Actor(), lambda query: [] if missing in query else query_complete(query))
    assert result["telemetry_complete"] is False
    assert result["error_ratio"] is None


@pytest.mark.parametrize("failure", ["old", "scrape_down", "partial"])
def test_old_down_or_partial_pod_telemetry_is_rejected(failure):
    def query(expression):
        rows = query_complete(expression)
        if expression.startswith("up{") and failure == "scrape_down":
            return [({"pod": "checkout-a"}, 0), ({"pod": "checkout-b"}, 1)]
        if expression.startswith("max by(pod)") and failure == "old":
            return [({"pod": pod}, 60) for pod in Actor().telemetry_pods()]
        if "dependency_errors_total" in expression and failure == "partial":
            return rows[:1]
        return rows

    assert gather("demo", Actor(), query)["telemetry_complete"] is False
