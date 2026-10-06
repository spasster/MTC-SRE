"""Проверка Kubernetes API внутри тестового healer; исходно нужны две реплики."""

import json

from app.k8s import ActionSkipped, K8sActuator


actor = K8sActuator()
if actor.replicas("demo", "checkout") != 2:
    raise SystemExit("Проверка не начата: исходное число реплик должно быть 2")
report = {"passed": False, "dry_run_preserves_replicas": False, "unguarded_downscale_rejected": False,
          "stale_replica_count_rejected": False, "original_replicas_restored": False}
applied = False
try:
    actor.scale("demo", "checkout", 5, dry_run=True)
    report["dry_run_preserves_replicas"] = actor.replicas("demo", "checkout") == 2
    actor.scale("demo", "checkout", 5)
    applied = True
    for expected, field in [(None, "unguarded_downscale_rejected"), (4, "stale_replica_count_rejected")]:
        try:
            actor.scale("demo", "checkout", 2, expected_replicas=expected)
        except ActionSkipped:
            report[field] = actor.replicas("demo", "checkout") == 5
finally:
    if applied:
        actor.scale("demo", "checkout", 2, expected_replicas=5)
    report["original_replicas_restored"] = actor.replicas("demo", "checkout") == 2
    report["passed"] = all(value for key, value in report.items() if key != "passed")
    print(json.dumps(report))
if not report["passed"]:
    raise SystemExit(1)
