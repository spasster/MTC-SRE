"""Повторное построение графиков из завершенных локальных измерений этой итерации."""

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.unicode_minus": False})


def read(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def capacity():
    report = read("capacity-client-reuse.json")
    if not report.get("finished_at"):
        raise RuntimeError("Замеры мощности еще не завершены; итоговый график не строится")
    fig, axes = plt.subplots(4, 1, figsize=(10, 10), sharex=True)
    fields = ("client_p95_ms", "actual_client_rps", "checkout_cpu_cores_max", "checkout_ram_bytes_max")
    for replicas, color in ((2, "#31688e"), (5, "#159947")):
        stages = [row for row in report["stages"] if row["replicas"] == replicas and "client" in row]
        for axis, field, factor in zip(axes, fields, (1, 1, 1, 1 / 2**20)):
            axis.plot([row["planned_rps"] for row in stages],
                      [row[field] * factor if row.get(field) is not None else float("nan") for row in stages],
                      marker="o", color=color, label=f"{replicas} реплики checkout")
            for row in stages:
                if not row["passed"] and row.get(field) is not None:
                    axis.scatter(row["planned_rps"], row[field] * factor, marker="x", s=100, color="#bb3333", zorder=4)
    axes[0].axhline(300, color="#bb3333", linestyle="--", label="Порог SLO 300 мс")
    axes[0].legend()
    axes[0].set_title(f"Общий HTTP-клиент checkout, две payment; ступени по {report['duration_seconds']} с")
    for axis, label in zip(axes, ("p95 клиента, мс", "Отправлено запросов/с", "CPU checkout, ядра", "RAM checkout, МиБ")):
        axis.set_ylabel(label)
        axis.grid(alpha=.2)
    axes[-1].set_xlabel("Заданный поток, RPS; красный крест - первый отказ; CPU/RAM только checkout")
    fig.tight_layout()
    fig.savefig(ROOT / "capacity-client-reuse.png", dpi=160)
    plt.close(fig)


def spike():
    report = read("spike-client-reuse.json")
    fig, axes = plt.subplots(4, 1, figsize=(10, 10), sharex=True)
    for axis, key, factor, label in zip(axes, ("p95", "request_rate", "error_ratio", "replicas"),
        (1000, 1, 100, 1), ("p95 сервиса, мс", "Завершено запросов/с", "Ошибки сервиса, %", "Реплики checkout")):
        rows = [row for row in report["samples"] if row.get(key) is not None]
        times = [row["time"] - report["injected_at"] for row in rows]
        values = [row[key] * factor for row in rows]
        axis.step(times, values, where="post") if key == "replicas" else axis.plot(times, values)
        axis.axvline(report["api_acceptance_seconds"], color="#159947", linestyle=":", label="API принял scale")
        axis.axvline(report["mttr_seconds"], color="#31688e", linestyle="--", label="SLO подтвержден")
        axis.set_ylabel(label)
        axis.grid(alpha=.2)
    axes[0].axhline(300, color="#bb3333", label="Порог SLO 300 мс")
    axes[0].set_yscale("log")
    axes[0].set_title("18 дополнительных + 3 фоновых RPS: деградация, восстановление и возврат масштаба")
    axes[0].legend(fontsize=9)
    axes[2].axhline(.5, color="#bb3333")
    axes[3].set_yticks([2, 5])
    axes[-1].set_xlabel("Секунды после инъекции; начальные отказы и перегрузка сохранены")
    fig.tight_layout()
    fig.savefig(ROOT / "spike-client-reuse.png", dpi=160)
    plt.close(fig)


if __name__ == "__main__":
    capacity()
    spike()
    print("Графики завершенных исходов построены; исходные JSON не изменены")
