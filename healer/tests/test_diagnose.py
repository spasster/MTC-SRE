from app.diagnose import decide

RULES = {
    "revision_window_seconds": 600,
    "error_ratio": 0.005,
    "dependency_ratio": 0.2,
    "slow_seconds": 0.3,
    "slow_ratio": 3,
    "cpu_throttle": 0.4,
    "scale_to": 5,
    "scale_from": 2,
    "max_actions": 2,
}


def evidence(**overrides):
    base = {
        "replicas": 2,
        "revision_age_seconds": 5000,
        "error_ratio": 0.0,
        "application_error_ratio": 0.0,
        "payment_error_ratio": 0.0,
        "redis_error_ratio": 0.0,
        "payment_timeout_ratio": 0.0,
        "redis_ready": True,
        "payment_ready": True,
        "pods": [
            {"pod": "a", "p95": 0.05, "throttle": 0.0},
            {"pod": "b", "p95": 0.06, "throttle": 0.0},
        ],
    }
    base.update(overrides)
    return base


def test_bad_release_beats_a_slow_pod():
    found = decide(
        evidence(revision_age_seconds=120, error_ratio=0.4, application_error_ratio=0.4,
                 pods=[{"pod": "a", "p95": 2.0, "throttle": 0.0}]),
        RULES,
    )
    assert found.diagnosis == "bad_release"
    assert found.mode == "approval"
    assert found.action == "rollback"


def test_redis_needs_approval():
    found = decide(evidence(redis_error_ratio=0.9), RULES)
    assert found.diagnosis == "redis"
    assert found.mode == "approval"
    assert "корзину" in found.detail


def test_redis_not_ready_is_not_a_restart():
    found = decide(evidence(redis_error_ratio=0.9, redis_ready=False), RULES)
    assert found.diagnosis != "redis"


def test_partition_does_not_touch_the_cluster():
    found = decide(evidence(payment_timeout_ratio=1.0), RULES)
    assert found.diagnosis == "partition"
    assert found.mode == "escalate"
    assert found.action == "none"


def test_one_slow_pod():
    found = decide(
        evidence(
            pods=[
                {"pod": "fast", "p95": 0.04, "throttle": 0.0},
                {"pod": "slow", "p95": 2.0, "throttle": 0.1},
            ]
        ),
        RULES,
    )
    assert found.diagnosis == "slow_pod"
    assert found.mode == "auto"
    assert found.action == "delete_pod"
    assert found.target == "slow"


def test_two_slow_pods_are_not_one_sick_pod():
    found = decide(
        evidence(
            replicas=5,
            p95=0.5,
            pods=[
                {"pod": "new", "p95": 0.08, "p95_recent": 0.08, "throttle": 0.02},
                {"pod": "old-a", "p95": 0.33, "p95_recent": 0.16, "throttle": 0.3},
                {"pod": "old-b", "p95": 0.50, "p95_recent": 0.87, "throttle": 0.38},
            ],
        ),
        RULES,
    )
    assert found.action != "delete_pod"
    assert found.diagnosis == "cpu"
    assert found.mode == "escalate"


def test_cpu_scales_when_throttle_is_just_under_half():
    found = decide(
        evidence(
            pods=[
                {"pod": "a", "p95": 0.94, "p95_recent": 1.4, "throttle": 0.44},
                {"pod": "b", "p95": 0.95, "p95_recent": 1.3, "throttle": 0.70},
            ]
        ),
        RULES,
    )
    assert found.diagnosis == "cpu"
    assert found.action == "scale"


def test_cpu_scales_until_the_cap():
    pods = [
        {"pod": "a", "p95": 1.2, "throttle": 0.8},
        {"pod": "b", "p95": 1.1, "throttle": 0.7},
    ]
    found = decide(evidence(pods=pods, replicas=2), RULES)
    assert found.diagnosis == "cpu"
    assert found.action == "scale"
    assert found.target == "5"
    capped = decide(evidence(pods=pods, replicas=5), RULES)
    assert capped.mode == "escalate"


def test_cpu_scales_when_every_pod_is_throttled():
    found = decide(
        evidence(
            pods=[
                {"pod": "restarted", "p95": 0.1, "p95_recent": 0.1, "throttle": 0.9},
                {"pod": "busy", "p95": 2.0, "p95_recent": 2.0, "throttle": 0.8},
            ]
        ),
        RULES,
    )
    assert found.diagnosis == "cpu"
    assert found.action == "scale"


def test_cap_escalates_when_one_pod_is_idle():
    found = decide(
        evidence(
            replicas=5,
            p95=2.0,
            pods=[
                {"pod": "idle", "p95": 0.1, "p95_recent": 0.1, "throttle": 0.03},
                {"pod": "hot", "p95": 5.0, "p95_recent": 5.0, "throttle": 0.98},
                {"pod": "new", "p95": 2.0, "p95_recent": 2.0, "throttle": 0.9},
            ],
        ),
        RULES,
    )
    assert found.diagnosis == "cpu"
    assert found.mode == "escalate"
    assert found.action == "none"


def test_slow_pod_beats_the_cap():
    found = decide(
        evidence(
            replicas=5,
            p95=1.0,
            pods=[
                {"pod": "fast", "p95": 0.05, "p95_recent": 0.05, "throttle": 0.0},
                {"pod": "slow", "p95": 2.0, "p95_recent": 2.0, "throttle": 0.1},
            ],
        ),
        RULES,
    )
    assert found.diagnosis == "slow_pod"
    assert found.target == "slow"


def test_recent_fast_pods_stay_quiet_at_the_cap():
    found = decide(
        evidence(
            replicas=5,
            p95=2.0,
            pods=[
                {"pod": "a", "p95": 2.0, "p95_recent": 0.05, "throttle": 0.9},
                {"pod": "b", "p95": 2.0, "p95_recent": 0.05, "throttle": 0.9},
            ],
        ),
        RULES,
    )
    assert found.diagnosis == "settling"


def test_unknown_escalates():
    found = decide(
        evidence(
            pods=[
                {"pod": "a", "p95": 0.5, "throttle": 0.1},
                {"pod": "b", "p95": 0.45, "throttle": 0.1},
            ]
        ),
        RULES,
    )
    assert found.diagnosis == "unknown"
    assert found.mode == "escalate"


def test_recovered_pod_is_not_deleted():
    found = decide(
        evidence(
            pods=[
                {"pod": "fast", "p95": 0.05, "p95_recent": 0.05, "throttle": 0.0},
                {"pod": "was-slow", "p95": 2.0, "p95_recent": 0.08, "throttle": 0.0},
            ]
        ),
        RULES,
    )
    assert found.diagnosis == "settling"
    assert found.mode == "ignore"
    assert found.action == "none"


def test_dead_series_does_not_hide_a_live_slow_pod():
    found = decide(
        evidence(
            pods=[
                {"pod": "gone", "p95": 4.0, "p95_recent": None, "throttle": 0.0},
                {"pod": "fast", "p95": 0.05, "p95_recent": 0.05, "throttle": 0.0},
                {"pod": "slow", "p95": 2.0, "p95_recent": 2.0, "throttle": 0.1},
            ]
        ),
        RULES,
    )
    assert found.diagnosis == "slow_pod"
    assert found.target == "slow"


def test_missing_evidence_escalates():
    found = decide({"error": "prometheus down"}, RULES)
    assert found.mode == "escalate"
    assert found.action == "none"


def test_payment_http_errors_do_not_roll_back_checkout():
    found = decide(evidence(revision_age_seconds=30, error_ratio=0.8,
                            payment_error_ratio=0.8), RULES)
    assert found.diagnosis == "payment"
    assert found.mode == "escalate"
    assert found.action == "none"


def test_mixed_application_and_dependency_errors_do_not_roll_back():
    found = decide(evidence(revision_age_seconds=30, error_ratio=0.1,
                            application_error_ratio=0.08, payment_error_ratio=0.02), RULES)
    assert found.action == "none"


def test_unattributed_errors_are_not_a_bad_release_or_recovery():
    found = decide(evidence(revision_age_seconds=30, error_ratio=0.8), RULES)
    assert found.diagnosis == "unknown"
    assert found.mode == "escalate"
