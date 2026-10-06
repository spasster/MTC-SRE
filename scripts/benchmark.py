"""Повторные парные измерения MTTR с сохранением всех исходов и среды."""

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from experiment import cmd


def describe(values):
    return {"count": len(values), "median": statistics.median(values) if values else None,
            "min": min(values) if values else None, "max": max(values) if values else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default=os.getenv("CLUSTER", "mtc"))
    parser.add_argument("--pairs", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20261007)
    parser.add_argument("--output-dir", default="docs/results/repeated-mttr")
    args = parser.parse_args()
    if args.pairs < 5:
        parser.error("Для этой серии требуется не менее пяти пар")
    os.environ["CLUSTER"] = args.cluster
    cmd("sh", "scripts/require-context.sh", args.cluster)
    directory = Path(args.output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / "summary.json").exists():
        raise SystemExit("Каталог содержит предыдущую серию; выбрать новый каталог")
    nodes = json.loads(cmd("kubectl", "get", "nodes", "-o", "json"))["items"]
    pods = [pod for namespace in ("demo", "heal")
            for pod in json.loads(cmd("kubectl", "get", "pods", "-n", namespace, "-o", "json"))["items"]]
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "source_commit": cmd("git", "rev-parse", "HEAD").strip(),
              "seed": args.seed, "planned_pairs": args.pairs, "pairs": [], "passed": False,
              "method": "Одинаковая инъекция задержки; порядок baseline/auto перемешан внутри каждой пары. Задержка оператора baseline равна 30 с после firing, не измерению работы человека.",
              "environment": {"cpu_available_to_process": len(os.sched_getaffinity(0)),
                              "nodes": [{"capacity": node["status"]["capacity"], "allocatable": node["status"]["allocatable"],
                                         "kubelet": node["status"]["nodeInfo"]["kubeletVersion"]} for node in nodes],
                              "images": sorted({item["imageID"] for pod in pods for item in pod.get("status", {}).get("containerStatuses", [])})}}
    randomizer = random.Random(args.seed)
    baseline, automatic, savings = [], [], []
    try:
        for pair_index in range(1, args.pairs + 1):
            order = ["baseline", "auto"]
            randomizer.shuffle(order)
            pair = {"index": pair_index, "order": order, "runs": {}}
            report["pairs"].append(pair)
            for mode in order:
                file = directory / f"pair-{pair_index}-{mode}.json"
                print(f"Пара {pair_index}/{args.pairs}: {mode}", flush=True)
                result = subprocess.run([sys.executable, "scripts/experiment.py", "--cluster", args.cluster,
                                         "--scenario", "slow-pod", "--mode", mode, "--output", str(file)],
                                        text=True, capture_output=True)
                saved = json.loads(file.read_text(encoding="utf-8")) if file.exists() else {"passed": False, "error": "Исходный отчет не создан"}
                pair["runs"][mode] = {"file": file.name, "exit_code": result.returncode, "passed": saved["passed"],
                                      "mttr_seconds": saved.get("mttr_seconds"), "error": saved.get("error")}
                # Каждый исход сохраняется сразу, включая ошибки и незавершенную пару.
                (directory / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                if not saved["passed"]:
                    print("Прогон не прошел; исход сохранен", flush=True)
            if all(pair["runs"][mode]["passed"] for mode in order):
                before, after = pair["runs"]["baseline"]["mttr_seconds"], pair["runs"]["auto"]["mttr_seconds"]
                baseline.append(before)
                automatic.append(after)
                savings.append(before - after)
    finally:
        valid = len(savings)
        report.update(finished_at=datetime.now(timezone.utc).isoformat(), valid_pairs=valid,
                      incomplete_or_failed_pairs=args.pairs - valid, baseline_seconds=describe(baseline), auto_seconds=describe(automatic),
                      paired_saving_seconds=describe(savings), passed=valid == args.pairs,
                      limitation="Пять пар описывают этот контур. p95 MTTR и промышленная мощность по такой выборке не заявляются.")
        (directory / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    if not report["passed"]:
        raise SystemExit("Серия включает неуспешные или незавершенные пары; см. summary.json")
    print("Завершена серия из пяти и более пар; все исходы сохранены", flush=True)


if __name__ == "__main__":
    main()
