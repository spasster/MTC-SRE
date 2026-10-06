"""Измерить границу SLO для фиксированного числа реплик и сохранить ошибки клиента."""

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from experiment import cmd, deployment, metrics, scalar, healthy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default=os.getenv("CLUSTER", "mtc"))
    parser.add_argument("--rates", default="1,3,4,6,8,12")
    parser.add_argument("--duration", type=int, default=120)
    parser.add_argument("--output", default="docs/results/capacity.json")
    args = parser.parse_args()
    rates = [int(item) for item in args.rates.split(",")]
    if rates != sorted(set(rates)) or rates[0] <= 0 or args.duration < 90:
        parser.error("Нужны возрастающие положительные RPS и длительность не менее 90 с")
    os.environ["CLUSTER"] = args.cluster
    cmd("sh", "scripts/require-context.sh", args.cluster)
    if json.loads(cmd("kubectl", "get", "networkchaos,podchaos", "-n", "demo", "-o", "json"))["items"]:
        raise SystemExit("Сначала завершить инъекцию Chaos Mesh")
    if any(row["metadata"]["name"] == "k6-spike" for row in json.loads(cmd("kubectl", "get", "jobs", "-n", "demo", "-o", "json"))["items"]):
        raise SystemExit("В demo уже есть Job k6-spike")
    original = {("heal", "healer"): deployment("healer", "heal")["spec"]["replicas"],
                ("demo", "checkout"): deployment("checkout", "demo")["spec"]["replicas"],
                ("demo", "k6-background"): deployment("k6-background", "demo")["spec"]["replicas"]}
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "source_commit": cmd("git", "rev-parse", "HEAD").strip(),
              "duration_seconds": args.duration, "stages": [], "bounds": {}, "passed": False,
              "method": "Фоновая нагрузка и healer выключены на время серии; 2 и 5 реплик проверяются при одинаковом профиле. Каждый RPS держится не менее 90 с. Результаты относятся только к этому контуру.",
              "checkout_resources_per_replica": deployment("checkout", "demo")["spec"]["template"]["spec"]["containers"][0]["resources"]}
    try:
        for (namespace, name) in [("heal", "healer"), ("demo", "k6-background")]:
            cmd("kubectl", "scale", "deployment/" + name, "-n", namespace, "--replicas=0")
        for replicas in (2, 5):
            cmd("kubectl", "scale", "deployment/checkout", "-n", "demo", "--replicas=" + str(replicas))
            cmd("kubectl", "rollout", "status", "deployment/checkout", "-n", "demo", "--timeout=180s")
            bound = {"highest_tested_passing_rps": None, "first_tested_failing_rps": None}
            report["bounds"][str(replicas)] = bound
            for rate in rates:
                # Перед каждой ступенью убираются очереди предыдущего клиента.
                time.sleep(20)
                manifest = json.loads(cmd("kubectl", "create", "-f", "k8s/k6-spike.yaml", "--dry-run=client", "-o", "json"))
                container = manifest["spec"]["template"]["spec"]["containers"][0]
                container["env"] += [{"name": "RATE", "value": str(rate)}, {"name": "DURATION", "value": str(args.duration) + "s"}]
                import subprocess
                applied = subprocess.run(["kubectl", "apply", "-f", "-"], input=json.dumps(manifest), text=True, capture_output=True)
                if applied.returncode:
                    raise RuntimeError("Ступень нагрузки не создана")
                stage = {"replicas": replicas, "planned_rps": rate, "samples": [], "passed": False}
                report["stages"].append(stage)
                print(f"Измеряется {rate} RPS при {replicas} репликах", flush=True)
                deadline = time.time() + args.duration + 90
                while time.time() < deadline:
                    sample = metrics("http://127.0.0.1:19090")
                    sample["checkout_cpu_cores"] = scalar("http://127.0.0.1:19090", 'sum(rate(container_cpu_usage_seconds_total{namespace="demo",container="checkout",pod=~"checkout-.*"}[1m]))')
                    sample["checkout_ram_bytes"] = scalar("http://127.0.0.1:19090", 'sum(container_memory_working_set_bytes{namespace="demo",container="checkout",pod=~"checkout-.*"})')
                    stage["samples"].append(sample)
                    job = json.loads(cmd("kubectl", "get", "job", "k6-spike", "-n", "demo", "-o", "json"))
                    if job.get("status", {}).get("succeeded") or job.get("status", {}).get("failed"):
                        break
                    time.sleep(10)
                else:
                    raise RuntimeError("Job ступени не завершился вовремя")
                logs = cmd("kubectl", "logs", "job/k6-spike", "-n", "demo", "-c", "k6")
                summaries = [json.loads(line[len("MTC_SUMMARY "):]) for line in logs.splitlines() if line.startswith("MTC_SUMMARY ")]
                if not summaries:
                    raise RuntimeError("Итог k6 не получен")
                summary = stage["client"] = summaries[-1]
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
                cmd("kubectl", "delete", "job", "k6-spike", "-n", "demo", "--wait=true")
                Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                if stage["passed"]:
                    bound["highest_tested_passing_rps"] = rate
                else:
                    bound["first_tested_failing_rps"] = rate
                    break
        report["passed"] = all(bound["highest_tested_passing_rps"] is not None for bound in report["bounds"].values())
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        cmd("kubectl", "delete", "job", "k6-spike", "-n", "demo", "--ignore-not-found")
        for (namespace, name), replicas in original.items():
            cmd("kubectl", "scale", "deployment/" + name, "-n", namespace, "--replicas=" + str(replicas))
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["limitation"] = "Указаны проверенные прошедшие ступени и первые отказы. Если одной границы нет, интервал максимума не установлен. Точный максимум и минимальные CPU/RAM этим опытом не доказаны."
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    if not report["passed"]:
        raise SystemExit(report.get("error", "Проходящие ступени не подтверждены для обоих вариантов"))
    print("Границы измеренной мощности и ресурсы сохранены", flush=True)


if __name__ == "__main__":
    main()
