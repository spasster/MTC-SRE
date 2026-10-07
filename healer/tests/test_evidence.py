import pytest

from app.evidence import gather, parse_vector
from app import evidence as module


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


def test_all_queries_in_a_snapshot_share_one_evaluation_time(monkeypatch):
    moments = []
    def query(expression, evaluation_time=None):
        moments.append(evaluation_time)
        return query_complete(expression)
    monkeypatch.setattr(module, "query_prometheus", query)
    assert gather("demo", Actor())["telemetry_complete"]
    assert len(moments) > 10 and len(set(moments)) == 1


def test_ratio_only_tolerates_float_roundoff():
    assert module._ratio(3.0000000000000004, 3) == 1
    assert module._ratio(3.1, 3) is None
    assert module._ratio(None, 3) is None


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


def test_collection_deadline_stops_queries_and_prevents_action_and_recovery(monkeypatch):
    from app import main
    from app.diagnose import decide

    clock, evaluations = [1000.0], []
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    def slow_query(expression, evaluation_time=None):
        evaluations.append(evaluation_time)
        clock[0] += 4
        rows = query_complete(expression)
        if expression.startswith("histogram_quantile") or "container_cpu" in expression:
            return [(labels, .5) for labels, value in rows]
        return rows

    monkeypatch.setattr(module, "query_prometheus", slow_query)
    result = gather("demo", Actor())
    limits = main.load_rules()
    assert not result["telemetry_complete"] and "истек" in result["error"]
    assert evaluations == [1000] * 5 and clock[0] == 1020
    assert decide(result, limits).action == "none"
    assert not main._healthy(result, limits)


@pytest.mark.parametrize("old_series", ["pods", "synthetic"])
def test_samples_that_expire_during_collection_are_rejected(monkeypatch, old_series):
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    def aging_query(expression):
        clock[0] += .1
        rows = query_complete(expression)
        if old_series == "pods" and expression.startswith("max by(pod)"):
            return [(labels, 44) for labels, value in rows]
        if old_series == "synthetic" and expression.startswith("max(time()"):
            return [({}, 44)]
        return rows

    result = gather("demo", Actor(), aging_query)
    assert clock[0] < module.MAX_COLLECTION_SECONDS
    assert not result["telemetry_complete"] and "устарели" in result["error"]


def test_final_ages_include_query_and_kubernetes_read_time(monkeypatch):
    clock, expressions = [0.0], []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    class SlowActor(Actor):
        def ready(self, *args):
            clock[0] += 1
            return True

    def query(expression):
        expressions.append(expression)
        clock[0] += .1
        return query_complete(expression)

    result = gather("demo", SlowActor(), query)
    duration = len(expressions) * .1 + 2
    assert result["telemetry_complete"]
    assert result["collection_seconds"] == pytest.approx(duration)
    assert result["telemetry_age_seconds"] == pytest.approx(5 + duration)
    assert result["synthetic_age_seconds"] == pytest.approx(5 + duration)


def test_late_kubernetes_read_prevents_remaining_reads(monkeypatch):
    clock, calls = [0.0], []
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    class SlowActor(Actor):
        def revision_age_seconds(self, *args):
            clock[0] = 21
            return 5000

        def replicas(self, *args):
            calls.append("replicas")
            return 2

    result = gather("demo", SlowActor(), query_complete)
    assert not result["telemetry_complete"] and calls == []


@pytest.mark.parametrize("missing", ["success", "age"])
def test_missing_synthetic_series_is_not_a_complete_snapshot(missing):
    def query(expression):
        prefix = "min(probe_success" if missing == "success" else "max(time()"
        return [] if expression.startswith(prefix) else query_complete(expression)

    assert not gather("demo", Actor(), query)["telemetry_complete"]
