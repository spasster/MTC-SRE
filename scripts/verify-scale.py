"""Проверка Kubernetes API внутри тестового healer; исходно нужны две реплики."""

import json
import time

from kubernetes.client.rest import ApiException

from app.k8s import ActionSkipped, K8sActuator


CHECKS = ("dry_run_preserves_replicas", "unguarded_downscale_rejected", "stale_replica_count_rejected", "original_replicas_restored")


def checked_scale(actor, replicas, identity, current, report, phase, dry_run=False):
    """Повторяется только отклоненный CAS; каждое чтение снова проверяет владельца."""
    for attempt in range(3):
        try:
            return actor.scale("demo", "checkout", replicas, dry_run=dry_run,
                               expected_replicas=current, expected_identity=identity)
        except ApiException as error:
            if error.status != 409 or attempt == 2:
                raise
            report["conflicts"][phase] += 1
            time.sleep(.2)


def verify(actor):
    report = {"passed": False, **dict.fromkeys(CHECKS, False), "errors": [], "cleanup_errors": [],
              "conflicts": {"dry_run": 0, "apply": 0, "cleanup": 0}, "max_attempts_per_phase": 3}
    original, owned, applied, phase = None, None, False, "initial"
    try:
        original = actor.deployment_identity("demo", "checkout")
        if actor.replicas("demo", "checkout") != 2:
            raise ActionSkipped("Исходное число реплик должно быть 2")
        phase = "dry_run"
        checked_scale(actor, 5, original, 2, report, phase, dry_run=True)
        report["dry_run_preserves_replicas"] = actor.replicas("demo", "checkout") == 2
        phase = "apply"
        checked_scale(actor, 5, original, 2, report, phase)
        applied = True
        owned = {**original, "generation": original["generation"] + 1}
        if actor.deployment_identity("demo", "checkout") != owned:
            raise ActionSkipped("Deployment изменен после собственной команды")
        phase = "guards"
        for expected, field in [(None, "unguarded_downscale_rejected"), (4, "stale_replica_count_rejected")]:
            try:
                actor.scale("demo", "checkout", 2, expected_replicas=expected)
            except ActionSkipped:
                report[field] = actor.replicas("demo", "checkout") == 5
    except Exception as error:
        report["errors"].append({"phase": phase, "error_type": type(error).__name__})
    finally:
        if applied and owned is not None:
            try:
                checked_scale(actor, 2, owned, 5, report, "cleanup")
            except Exception as error:
                report["cleanup_errors"].append({"phase": "cleanup", "error_type": type(error).__name__})
        try:
            report["original_replicas_restored"] = (original is not None
                and actor.deployment_identity("demo", "checkout")["uid"] == original["uid"]
                and actor.replicas("demo", "checkout") == 2)
        except Exception as error:
            report["cleanup_errors"].append({"phase": "read_after_cleanup", "error_type": type(error).__name__})
    report["passed"] = all(report[key] for key in CHECKS) and not report["errors"] and not report["cleanup_errors"]
    return report


def main(actor=None):
    try:
        report = verify(actor or K8sActuator())
    except Exception as error:
        report = {"passed": False, "errors": [{"phase": "initialization", "error_type": type(error).__name__}]}
    print(json.dumps(report))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
