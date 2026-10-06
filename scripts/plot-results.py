"""Графики из сохранённых измерений; исходные результаты не изменяются."""

import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path("docs/results")
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11, "axes.unicode_minus": False})


def plot_iteration(directory):
    summary = json.loads((directory / "repeated-mttr/summary.json").read_text(encoding="utf-8"))
    fig, axis = plt.subplots(figsize=(9, 4.8))
    for pair in summary["pairs"]:
        runs = pair["runs"]
        if all(runs.get(mode, {}).get("passed") for mode in ("baseline", "auto")):
            axis.plot([0, 1], [runs["baseline"]["mttr_seconds"], runs["auto"]["mttr_seconds"]],
                      marker="o", label=f"Пара {pair['index']}", alpha=.8)
    axis.set(xticks=[0, 1], xticklabels=["Baseline: задержка оператора 30 с", "Автоматическое восстановление"],
             ylabel="MTTR от внесения сбоя, секунды", title="Один медленный под: пять пар в одинаковом контуре")
    axis.text(.5, .04, f"Медианы: {summary['baseline_seconds']['median']:.1f} -> {summary['auto_seconds']['median']:.1f} с; "
              f"медиана экономии в паре {summary['paired_saving_seconds']['median']:.1f} с",
              transform=axis.transAxes, ha="center", fontsize=10)
    axis.grid(axis="y", alpha=.2)
    axis.legend(loc="upper right")
    fig.tight_layout()
    fig.savefig(directory / "mttr-pairs.png", dpi=160)
    plt.close(fig)

    capacity_file = directory / "capacity.json"
    if not capacity_file.exists():
        return
    report = json.loads(capacity_file.read_text(encoding="utf-8"))
    if not report.get("finished_at"):
        return
    fig, axes = plt.subplots(4, 1, figsize=(10, 10), sharex=True)
    for replicas, color in ((2, "#31688e"), (5, "#159947")):
        stages = [stage for stage in report["stages"] if stage["replicas"] == replicas and "client" in stage]
        fields = ["client_p95_ms", "actual_client_rps", "checkout_cpu_cores_max", "checkout_ram_bytes_max"]
        for axis, field, factor in zip(axes, fields, (1, 1, 1, 1 / 2**20)):
            axis.plot([stage["planned_rps"] for stage in stages],
                      [stage.get(field) * factor if stage.get(field) is not None else float("nan") for stage in stages],
                      marker="o", color=color, label=f"{replicas} реплики")
            for stage in stages:
                if not stage["passed"] and stage.get(field) is not None:
                    axis.scatter(stage["planned_rps"], stage[field] * factor, marker="x", s=100, color="#bb3333", zorder=4)
    axes[0].axhline(300, color="#bb3333", linestyle="--", label="SLO 300 мс")
    axes[0].set_title(f"Ступени по {report['duration_seconds']} с: реальные запросы k6 и ресурсы checkout")
    for axis, label in zip(axes, ("p95 клиента, мс", "Отправлено запросов/с", "Максимум CPU, ядра", "Максимум RAM, МиБ")):
        axis.set_ylabel(label)
        axis.grid(alpha=.2)
    axes[0].legend()
    axes[-1].set_xlabel("Заданный поток, запросов/с; красный крест - ступень не прошла критерии")
    fig.tight_layout()
    fig.savefig(directory / "capacity-stages.png", dpi=160)
    plt.close(fig)
    print("Графики пар MTTR и ступеней мощности построены из сохраненных исходов.")


def read(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def series(report, key, scale=1):
    rows = [row for row in report["samples"] if row.get(key) is not None]
    return ([row["time"] - report["injected_at"] for row in rows], [row[key] * scale for row in rows])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iteration-dir", type=Path)
    args = parser.parse_args()
    if args.iteration_dir:
        plot_iteration(args.iteration_dir)
        return
    fig, axis = plt.subplots(figsize=(10, 4.4))
    for name, label, color in [("slow-pod-baseline.json", "Baseline: задержка оператора 30 с", "#31688e"),
                               ("slow-pod-auto.json", "Автоматическое восстановление", "#159947")]:
        report = read(name)
        axis.plot(*series(report, "p95", 1000), label=f"{label}, MTTR {report['mttr_seconds']:.1f} с", color=color)
        axis.axvline(report["api_acceptance_seconds"], linestyle=":", color=color, alpha=.7)
    axis.axhline(300, color="#bb3333", linestyle="--", label="Порог SLO 300 мс")
    axis.set(xlabel="Секунды после внесения сбоя", ylabel="p95 GET /work, мс",
             title="Один медленный под: одинаковый контур, отдельные прогоны")
    axis.grid(alpha=.2)
    axis.legend(loc="upper right", fontsize=9)
    fig.tight_layout()
    fig.savefig(ROOT / "mttr.png", dpi=160)
    plt.close(fig)

    report = read("spike-auto.json")
    fig, axes = plt.subplots(4, 1, figsize=(10, 9), sharex=True)
    for axis, key, label, scale in zip(axes,
        ["p95", "request_rate", "cpu_throttle", "replicas"],
        ["p95, мс", "Завершённых запросов/с", "Доля CPU throttle", "Реплики checkout"], [1000, 1, 1, 1]):
        axis.plot(*series(report, key, scale), color="#31688e", marker=".")
        axis.axvline(report["api_acceptance_seconds"], color="#159947", linestyle=":")
        axis.set_ylabel(label)
        axis.grid(alpha=.2)
    axes[0].axhline(300, color="#bb3333", linestyle="--", label="SLO")
    axes[0].legend()
    axes[0].set_title("Нагрузка 6 запросов/с: масштабирование 2 -> 5 и подтверждение SLO")
    axes[2].set_ylim(0, 1)
    axes[3].set_yticks([2, 5])
    axes[-1].set_xlabel("Секунды после запуска нагрузки; пунктир - принятие команды API")
    fig.tight_layout()
    fig.savefig(ROOT / "capacity.png", dpi=160)
    plt.close(fig)

    report = read("spike-overload.json")
    fig, axes = plt.subplots(2, 1, figsize=(10, 5), sharex=True)
    axes[0].plot(*series(report, "p95"), color="#bb3333")
    axes[0].axhline(.3, color="#159947", linestyle="--")
    axes[0].set(ylabel="p95, секунды", title="Перегрузка 19 запросов/с: пять реплик не восстановили SLO")
    axes[1].step(*series(report, "replicas"), where="post")
    axes[1].set(ylabel="Реплики", xlabel="Секунды после запуска нагрузки", yticks=[2, 5])
    for axis in axes:
        axis.grid(alpha=.2)
    fig.tight_layout()
    fig.savefig(ROOT / "overload.png", dpi=160)
    plt.close(fig)
    print("Подготовлены графики MTTR, рабочей нагрузки и измеренного предела стенда.")


if __name__ == "__main__":
    main()
