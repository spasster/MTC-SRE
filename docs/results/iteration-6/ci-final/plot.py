"""Построить графики только по принятому полному CI, сохраняя прежние опыты."""
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[4]
TARGET = Path(__file__).resolve().parent
def read(name):
    return json.loads((TARGET / name).read_text(encoding="utf-8"))

audit, capacity, spike = read("audit.json"), read("cpu-capacity.json"), read("spike-lifecycle.json")
if audit["passed"] is not True:
    raise SystemExit("Нужна успешная сверка исходных результатов")
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11, "axes.unicode_minus": False, "axes.spines.top": False,
                     "axes.spines.right": False, "figure.facecolor": "white", "savefig.facecolor": "white"})
run = read("run.json")["run"]
footer = f"CI {run['id']} | {audit['source_commit'][:7]} | чистый Ubuntu | 7 октября 2026"

fig, axis = plt.subplots(figsize=(9, 5.6))
values = [audit["baseline_mttr_seconds"], audit["auto_mttr_seconds"]]
bars = axis.bar(["Baseline\nЗадержка оператора 30 с", "Автоматика"], values, width=.55,
                color=["#657789", "#2479bb"])
axis.set_ylim(0, max(values) * 1.25)
axis.set_ylabel("Время от инъекции до подтверждения SLO, с")
axis.set_title("Одна контролируемая пара: медленный под", loc="left", pad=18, fontweight="bold")
axis.grid(axis="y", alpha=.2)
axis.set_axisbelow(True)
for bar, value in zip(bars, values):
    axis.text(bar.get_x() + bar.get_width()/2, value + max(values)*.03, f"{value:.1f} с", ha="center", fontweight="bold")
fig.text(.08, .07, "Один опыт на режим. Это не p95 MTTR и не измерение работы человека.", fontsize=10)
fig.text(.08, .025, footer, fontsize=9, color="#536170")
fig.subplots_adjust(left=.14, right=.97, top=.86, bottom=.22)
fig.savefig(TARGET / "mttr-final.png", dpi=160)
plt.close(fig)

fig, axes = plt.subplots(2, 2, figsize=(12.5, 9.2))
for replicas, color in [(2, "#2479bb"), (5, "#d17b25")]:
    rows = sorted((row for row in capacity["stages"] if row["replicas"] == replicas), key=lambda row: row["planned_rps"])
    rates = [row["planned_rps"] for row in rows]
    metrics = [("client_p95_ms", "p95 полного клиента, мс"),
               ("dropped", "Пропущенные итерации k6, шт."),
               ("checkout_cpu_cores_max", "Максимум CPU checkout, ядра"),
               ("checkout_ram_bytes_max", "Максимум RAM checkout, МиБ")]
    for axis, (key, label) in zip(axes.flat, metrics):
        ys = [row.get(key) if row.get(key) is not None else np.nan for row in rows]
        if key == "checkout_ram_bytes_max":
            ys = [value/(1024**2) for value in ys]
        axis.plot(rates, ys, color=color, marker="o", linewidth=1.8, label="2 реплики" if replicas == 2 else "5 реплик")
        if key == "client_p95_ms":
            for row in rows:
                if row["passed"] is False and row.get(key) is not None:
                    axis.scatter([row["planned_rps"]], [row[key]], marker="x", color=color, s=100, zorder=4)
        axis.set_xlabel("Заданный поток, RPS")
        axis.set_ylabel(label)
        axis.set_xticks(sorted({row["planned_rps"] for row in capacity["stages"]}))
        axis.tick_params(axis="x", labelrotation=30)
        axis.grid(alpha=.2)
        axis.set_axisbelow(True)
for axis in (axes[0, 1], axes[1, 0], axes[1, 1]):
    # Верхняя граница определяется после обеих серий, чтобы пять реплик не обрезались.
    axis.set_ylim(bottom=0)
axes[0, 0].axhline(300, color="#a94d53", linestyle="--", label="Порог 300 мс")
axes[0, 0].set_yscale("log")
axes[0, 0].legend(fontsize=9)
axes[0, 1].legend(fontsize=9)
fig.suptitle("Ступени мощности одного CI-контура", x=.07, ha="left", fontweight="bold", fontsize=16)
fig.text(.07, .055, "120 с на ступень; исходная сетка и измеренное уточнение. Крестики обозначают непройденные критерии.", fontsize=10)
fig.text(.07, .032, "CPU/RAM относятся к контейнерам checkout. Короткая серия не определяет промышленный максимум.", fontsize=10)
fig.text(.07, .012, footer, fontsize=9, color="#536170")
fig.subplots_adjust(left=.085, right=.96, top=.91, bottom=.15, hspace=.38, wspace=.32)
fig.savefig(TARGET / "capacity-final.png", dpi=160)
plt.close(fig)

fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True, gridspec_kw={"height_ratios": [2, 1]})
start = spike["injected_at"]
samples = spike["samples"]
times = [row["time"] - start for row in samples]
def p95(row):
    value = row.get("p95")
    return value * 1000 if type(value) in (int, float) and math.isfinite(value) and value > 0 else np.nan
axes[0].plot(times, [p95(row) for row in samples], color="#2479bb", linewidth=2, marker=".", label="Серверный p95, окно 1 мин")
axes[0].axhline(300, color="#a94d53", linestyle="--", label="Порог 300 мс")
axes[0].set_yscale("log")
axes[0].set_ylabel("Задержка, мс")
axes[0].set_title(f"CPU {audit['cpu_profile_rps']} RPS: SLO подтвержден до окончания нагрузки", loc="left", pad=20, fontweight="bold")
axes[1].step(times, [row["replicas"] for row in samples], where="post", color="#657789", linewidth=2)
axes[1].set_ylabel("Реплики checkout")
axes[1].set_yticks([2, 3, 4, 5])
axes[1].set_ylim(1.7, 5.3)
axes[1].set_xlabel("Время после инъекции, с")
for key, title, color in [("detected_at", "Детект", "#a18223"), ("api_accepted_at", "API принял scale", "#8561a7"),
                           ("slo_restored_at", "SLO подтвержден", "#287e5d"), ("scale_down_at", "Возврат двух реплик", "#657789")]:
    elapsed = spike[key] - start
    for axis in axes:
        axis.axvline(elapsed, color=color, linewidth=1.3, linestyle=":")
    axes[0].plot([], [], color=color, linestyle=":", label=f"{title}: {elapsed:.1f} с")
for axis in axes:
    axis.grid(alpha=.2)
    axis.set_axisbelow(True)
axes[0].legend(loc="upper center", bbox_to_anchor=(.5, -.12), ncol=2, fontsize=9, frameon=False)
fig.text(.085, .055, "Три последовательных здоровых чтения с шагом около 5 с используют перекрывающиеся минутные окна.", fontsize=10)
fig.text(.085, .032, "Естественное завершение собственного Job и самостоятельный возврат масштаба подтверждены JSON.", fontsize=10)
fig.text(.085, .012, footer, fontsize=9, color="#536170")
fig.subplots_adjust(left=.1, right=.97, top=.88, bottom=.17, hspace=.64)
fig.savefig(TARGET / "cpu-recovery-final.png", dpi=160)
plt.close(fig)

paths = [TARGET / name for name in ("mttr-final.png", "capacity-final.png", "cpu-recovery-final.png")]
metadata = {"checked_at": datetime.now(timezone.utc).isoformat(), "source_commit": audit["source_commit"],
            "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "input_sha256": {name: hashlib.sha256((TARGET / name).read_bytes()).hexdigest()
                             for name in ("audit.json", "cpu-capacity.json", "spike-lifecycle.json")},
            "images_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths},
            "visual_review_passed": False, "scope": "Графики построены из исходных JSON заключительного CI; визуальная проверка фиксируется отдельно."}
(TARGET / "plots.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
print("Подготовлены три графика заключительного CI для визуальной проверки")
