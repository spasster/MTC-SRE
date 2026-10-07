"""Измерить границу SLO для фиксированного числа реплик и сохранить ошибки клиента."""

import argparse
import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from experiment import cmd, deployment, metrics, scalar, healthy, wait_incident_closed, OwnedDeployment, OwnedResource
from cpu_profile import stage_diagnostics


def job_summary(owned):
    """Итог читается только у пода созданного этой ступенью Job."""
    job = owned.current()
    if job is None:
        raise RuntimeError("Созданный Job исчез до чтения результата")
    pods = json.loads(cmd("kubectl", "get", "pods", "-n", owned.namespace, "-l", "job-name=" + owned.name, "-o", "json"))["items"]
    summaries = []
    for pod in pods:
        if any(owner.get("kind") == "Job" and owner.get("uid") == job["metadata"]["uid"] and owner.get("controller") is True
               for owner in pod["metadata"].get("ownerReferences", [])):
            logs = cmd("kubectl", "logs", "-n", owned.namespace, pod["metadata"]["name"], "-c", "k6", timeout=30)
            summaries.extend(json.loads(line[len("MTC_SUMMARY "):]) for line in logs.splitlines() if line.startswith("MTC_SUMMARY "))
    after = owned.current()
    if after is None or after["metadata"]["uid"] != job["metadata"]["uid"] or len(summaries) != 1:
        raise RuntimeError("Единственный итог собственного Job k6 не подтвержден")
    return summaries[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default=os.getenv("CLUSTER", "mtc"))
    parser.add_argument("--rates", default="1,3,4,6,8,12")
    parser.add_argument("--replicas", default="2,5", help="Проверяемые варианты: 2, 5 или 2,5")
    parser.add_argument("--continue-after-failure", action="store_true", help="Проверить все ступени для калибровки CPU-демо")
    parser.add_argument("--duration", type=int, default=120)
    parser.add_argument("--output", default="docs/results/capacity.json")
    args = parser.parse_args()
    rates = [int(item) for item in args.rates.split(",")]
    replica_counts = [int(item) for item in args.replicas.split(",")]
    if replica_counts != sorted(set(replica_counts)) or not set(replica_counts) <= {2, 5}:
        parser.error("Разрешены варианты с двумя и пятью репликами")
    if rates != sorted(set(rates)) or rates[0] <= 0 or args.duration < 90:
        parser.error("Нужны возрастающие положительные RPS и длительность не менее 90 с")
    os.environ["CLUSTER"] = args.cluster
    cmd("sh", "scripts/require-context.sh", args.cluster)
    if json.loads(cmd("kubectl", "get", "networkchaos,podchaos", "-n", "demo", "-o", "json"))["items"]:
        raise SystemExit("Сначала завершить инъекцию Chaos Mesh")
    if any(row["metadata"]["name"] == "k6-spike" for row in json.loads(cmd("kubectl", "get", "jobs", "-n", "demo", "-o", "json"))["items"]):
        raise SystemExit("В demo уже есть Job k6-spike")
    original_healer = deployment("healer", "heal")
    original_healer_image = next(row["image"] for row in original_healer["spec"]["template"]["spec"]["containers"] if row["name"] == "healer")
    originals = {("heal", "healer"): original_healer,
                 ("demo", "checkout"): deployment("checkout", "demo"),
                 ("demo", "k6-background"): deployment("k6-background", "demo")}
    marker = uuid.uuid4().hex
    owners = {key: OwnedDeployment(key[1], key[0], obj, marker) for key, obj in originals.items()}
    owned_jobs = []
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "source_commit": cmd("git", "rev-parse", "HEAD").strip(),
              "cluster": args.cluster,
              "verifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "duration_seconds": args.duration, "stages": [], "bounds": {}, "passed": False,
              "continue_after_failure": args.continue_after_failure,
              "method": "Фоновая нагрузка и healer выключены на время серии; 2 и 5 реплик проверяются при одинаковом профиле. Каждый RPS держится не менее 90 с. Результаты относятся только к этому контуру.",
              "checkout_resources_per_replica": deployment("checkout", "demo")["spec"]["template"]["spec"]["containers"][0]["resources"]}
    try:
        report["initial_incident"] = wait_incident_closed()
        for (namespace, name) in [("heal", "healer"), ("demo", "k6-background")]:
            owners[(namespace, name)].mutate(0)
        for replicas in replica_counts:
            owners[("demo", "checkout")].mutate(replicas)
            cmd("kubectl", "rollout", "status", "deployment/checkout", "-n", "demo", "--timeout=180s", timeout=210)
            bound = {"highest_tested_passing_rps": None, "first_tested_failing_rps": None}
            report["bounds"][str(replicas)] = bound
            for rate in rates:
                # Перед каждой ступенью убираются очереди предыдущего клиента.
                time.sleep(20)
                manifest = json.loads(cmd("kubectl", "create", "-f", "k8s/k6-spike.yaml", "--dry-run=client", "-o", "json"))
                container = manifest["spec"]["template"]["spec"]["containers"][0]
                container["env"] += [{"name": "RATE", "value": str(rate)}, {"name": "DURATION", "value": str(args.duration) + "s"}]
                owned = OwnedResource(manifest, marker)
                owned_jobs.append(owned)
                owned.create(timeout=30)
                stage = {"replicas": replicas, "planned_rps": rate, "samples": [], "passed": False, "job_uid": owned.uid}
                report["stages"].append(stage)
                print(f"Измеряется {rate} RPS при {replicas} репликах", flush=True)
                deadline = time.monotonic() + args.duration + 90
                while time.monotonic() < deadline:
                    before = owned.current()
                    if before is None:
                        raise RuntimeError("Собственный Job исчез до измерения")
                    sample = metrics("http://127.0.0.1:19090")
                    sample["checkout_cpu_cores"] = scalar("http://127.0.0.1:19090", 'sum(rate(container_cpu_usage_seconds_total{namespace="demo",container="checkout",pod=~"checkout-.*"}[1m]))')
                    sample["checkout_ram_bytes"] = scalar("http://127.0.0.1:19090", 'sum(container_memory_working_set_bytes{namespace="demo",container="checkout",pod=~"checkout-.*"})')
                    job = owned.current()
                    if job is None or time.monotonic() >= deadline:
                        raise RuntimeError("Собственный Job или своевременное завершение не подтверждены")
                    # Окно относится к нагрузке, только если свой Job активен до и после чтения.
                    sample["load_active"] = all(obj.get("status", {}).get("active", 0) > 0
                                                and not obj.get("status", {}).get("succeeded")
                                                and not obj.get("status", {}).get("failed") for obj in (before, job))
                    stage["samples"].append(sample)
                    if job.get("status", {}).get("succeeded") or job.get("status", {}).get("failed"):
                        break
                    time.sleep(10)
                else:
                    raise RuntimeError("Job ступени не завершился вовремя")
                summary = stage["client"] = job_summary(owned)
                sent = summary["http_reqs"].get("count", 0)
                drops = summary["dropped_iterations"].get("count", 0)
                errors = summary["http_req_failed"].get("rate", 1)
                duration = summary["http_req_duration"].get("p(95)")
                recent = stage["samples"][-3:]
                stage.update(planned_iterations=rate * args.duration, sent=sent, dropped=drops,
                             actual_client_rps=sent / args.duration, client_p95_ms=duration,
                             passed=(not job.get("status", {}).get("failed") and sent >= rate * args.duration * .99
                                     and drops == 0 and errors <= .005 and duration is not None and duration < 300
                                     and len(recent) == 3 and all(healthy(sample) for sample in recent)))
                for field in ("checkout_cpu_cores", "checkout_ram_bytes"):
                    values = [sample[field] for sample in stage["samples"] if sample[field] is not None]
                    stage[field + "_max"] = max(values) if values else None
                print("Измерена ступень: " + json.dumps(stage_diagnostics(stage, args.duration), ensure_ascii=False), flush=True)
                owned.cleanup()
                Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                if stage["passed"]:
                    bound["highest_tested_passing_rps"] = rate
                else:
                    if bound["first_tested_failing_rps"] is None:
                        bound["first_tested_failing_rps"] = rate
                    if not args.continue_after_failure:
                        break
        report["passed"] = all(bound["highest_tested_passing_rps"] is not None for bound in report["bounds"].values())
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        cleanup_errors = []
        def cleanup(operation, action):
            try:
                action()
            except Exception as error:
                cleanup_errors.append({"operation": operation, "error_type": type(error).__name__})
        for owned in reversed(owned_jobs):
            cleanup("job/" + owned.name, owned.cleanup)
        healer_owner = owners[("heal", "healer")]
        if healer_owner.changed and not healer_owner.unknown and original_healer["spec"]["replicas"] > 0:
            cleanup("импорт исходного healer", lambda: cmd("k3d", "image", "import", original_healer_image,
                                                            "-c", args.cluster, "--mode", "direct", timeout=180))
        for (namespace, name), owner in reversed(list(owners.items())):
            cleanup("deployment/" + namespace + "/" + name, owner.cleanup)
        report["cleanup_errors"] = cleanup_errors
        report["resource_intents"] = [{"kind": obj.kind, "name": obj.name, "namespace": obj.namespace,
                                       "uid_confirmed": obj.uid, "generation_confirmed": obj.generation} for obj in owned_jobs]
        if cleanup_errors:
            report["passed"] = False
            report.setdefault("error", "Очистка серии мощности не подтверждена")
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["limitation"] = "Указаны проверенные прошедшие ступени и первые отказы. Если одной границы нет или результаты немонотонны, интервал максимума не установлен. Точный максимум и минимальные CPU/RAM этим опытом не доказаны."
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    if not report["passed"]:
        raise SystemExit(report.get("error", "Проходящие ступени не подтверждены для выбранных вариантов"))
    print("Границы измеренной мощности и ресурсы сохранены", flush=True)


if __name__ == "__main__":
    main()
