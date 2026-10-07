"""Сверить сохраненные доказательства последней приемки без изменения контура."""
import hashlib
import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
RESULTS = Path(__file__).resolve().parent
SOURCE = "dae8ac7a1bd90dc01e0612a7b9a217c52c7fc1e8"
checks = []

def read(name):
    return json.loads((RESULTS / name).read_text(encoding="utf-8"))

def check(name, passed):
    checks.append({"name": name, "passed": bool(passed)})

def finite(value):
    return type(value) in (int, float) and math.isfinite(value)

def clean(row):
    return row.get("passed") is True and row.get("cleanup_errors", []) == []

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

run = read("run.json")
check("Полный CI завершен успешно на проверяемой версии", run["passed"] is True
      and run["run"]["head_sha"] == SOURCE and run["run"]["conclusion"] == "success")
check("Все обязательные стадии, сохранение результатов и удаление контура завершены", all(
    job["conclusion"] == "success" and all(step["conclusion"] == "success" for step in job["steps"] if not step["name"].startswith("Post "))
    for job in run["jobs"]))
cleanup = read("cleanup.json")
check("Удаление mtc-ci подтверждено сообщением k3d внутри финальной стадии", cleanup.get("passed") is True
      and cleanup.get("cluster_removal_confirmed") is True and cleanup.get("marker_count") == 1
      and cleanup.get("cluster") == "mtc-ci" and cleanup.get("source_commit") == SOURCE
      and cleanup.get("run_id") == run["run"]["id"]
      and any(job["id"] == cleanup["job_id"] and job["conclusion"] == "success" for job in run["jobs"]))
delivery = read("delivery.json")
check("Полученный архив совпадает с опубликованным SHA-256", delivery.get("digest_verified") is True
      and delivery.get("incomplete_json_count") == 0
      and any(row["id"] == delivery["artifact_id"] and row["digest"] == delivery["declared_digest"] for row in run["artifacts"]))
runtime_paths = [".github", "Makefile", "versions.mk", "apps", "healer", "scripts", "k8s",
                 "observability", "chaos", "k6", "ansible"]
changed = subprocess.check_output(["git", "diff", "--name-only", SOURCE, "HEAD", "--", *runtime_paths],
                                  cwd=ROOT, text=True, encoding="utf-8").splitlines()
dirty = subprocess.check_output(["git", "diff", "--name-only", "HEAD", "--", *runtime_paths],
                                cwd=ROOT, text=True, encoding="utf-8").splitlines()
check("Текущие код и инфраструктура совпадают с версией полного CI", not changed and not dirty)

for name in ["node-logs.json", "observability.json", "scale-guard.json", "rules.json", "healer-rbac.json",
             "limited-chaos.json", "healer-observability.json", "scenarios.json", "healer-state.json",
             "incident-chain.json", "mttr-baseline.json", "mttr-auto.json", "cpu-capacity.json",
             "spike-lifecycle.json", "runtime-images.json"]:
    check("Результат и очистка: " + name, clean(read(name)))

logs = read("node-logs.json")
check("Получены реальные контрольные события rsyslog и auditd", logs.get("syslog_marker_entries", 0) > 0
      and logs.get("audit_marker_entries", 0) > 0)
limited = read("limited-chaos.json")
check("Все три ограниченных воздействия подтвердили SLO и алерт", len(limited["cases"]) == 3
      and {row["fault"] for row in limited["cases"]} == {"pod", "network", "cpu"}
      and all(row["passed"] and row["slo_held_in_all_samples"] and row["alert_observed"] for row in limited["cases"]))
check("Исходный масштаб ограниченного хаоса возвращен", limited.get("original_checkout_replicas_restored") is True
      and limited.get("original_background_replicas_restored") is True)
nodata = read("healer-observability.json")
check("NoData не создал операцию и инцидент закрыт", nodata.get("new_operations") == 0
      and nodata.get("test_incident_closed") is True and nodata.get("closed_state", {}).get("incident_closed") is True
      and nodata.get("restored", {}).get("evidence_complete") == 1)
scenarios = read("scenarios.json")
check("Четыре сценария завершены и процесс остановлен", len(scenarios["cases"]) == 4
      and {row["scenario"] for row in scenarios["cases"]} == {"kill", "partition", "bad-release", "redis"}
      and all(row["passed"] for row in scenarios["cases"]) and scenarios.get("forward_stopped") is True
      and scenarios.get("unfinished_resources") == [])
state = read("healer-state.json")
check("Бюджет, cooldown и журнал сохранены после смены пода", all(state.get(key) is True
      for key in ("action_budget_preserved", "cooldown_preserved", "journal_preserved")))
check("Два экземпляра healer сохраняют одного владельца Lease", all(state.get(key) is True
      for key in ("two_instances", "single_holder", "extra_processes_refused")))
chain = read("incident-chain.json")
baseline, auto = read("mttr-baseline.json"), read("mttr-auto.json")
check("Метрика, трейс и оба журнала относятся к медленному запросу этой инъекции", chain.get("metric_exemplar_found") is True
      and set(chain.get("trace_services", [])) == {"checkout", "payment"}
      and set(chain.get("log_services", [])) == {"checkout", "payment"}
      and chain.get("incident_since") == baseline.get("injected_at")
      and chain["request"]["request_at"] >= baseline["injected_at"]
      and chain["request"]["duration_seconds"] >= .3)
for mode, row in [("baseline", baseline), ("auto", auto)]:
    check("Хронология MTTR " + mode, all(finite(row.get(key)) for key in (
        "injected_at", "detected_at", "api_accepted_at", "slo_restored_at", "mttr_seconds"))
        and row["injected_at"] <= row["api_accepted_at"] <= row["slo_restored_at"]
        and row["injected_at"] <= row["detected_at"] <= row["slo_restored_at"]
        and abs(row["mttr_seconds"] - (row["slo_restored_at"] - row["injected_at"])) < .001)

capacity, spike = read("cpu-capacity.json"), read("spike-lifecycle.json")
refinement = capacity.get("cpu_refinement", {})
added_rates = refinement.get("attempted_rates", [])
base_rates = (3, 9, 12, 15, 16, 18, 24)
measured_grid = [(row["replicas"], row["planned_rps"]) for row in capacity["stages"]]
expected_grid = {(replicas, rate) for replicas in (2, 5) for rate in (*base_rates, *added_rates)}
check("Мощность измерена на этой версии и в текущем CI-контуре", capacity["source_commit"] == SOURCE
      and capacity["cluster"] == "mtc-ci" and capacity["duration_seconds"] >= 90
      and capacity.get("continue_after_failure") is True
      and capacity["duration_seconds"] == 120)
check("Исходная сетка сохранена, уточнение ограничено тремя парными RPS", refinement.get("max_additional_rates") == 3
      and len(added_rates) <= 3 and len(set(added_rates)) == len(added_rates)
      and all(type(rate) is int and min(base_rates) < rate < max(base_rates) and rate not in base_rates for rate in added_rates)
      and len(measured_grid) == 14 + 2 * len(added_rates) and set(measured_grid) == expected_grid
      and len(set(measured_grid)) == len(measured_grid)
      and refinement.get("profile_found") is True
      and refinement.get("selected_rps") == spike.get("cpu_profile", {}).get("total_rps"))
check("После мощности подтвержден исходный контур и свежий SLO", capacity.get("baseline_reset", {}).get("confirmed_zero") is True
      and capacity.get("baseline_restored", {}).get("passed") is True
      and capacity["baseline_restored"].get("health_checked") is True
      and capacity.get("baseline_incident", {}).get("incident_closed") is True
      and len(capacity.get("baseline_health_samples", [])) >= 3
      and all(row.get("slo_healthy") is True and row.get("alerts_clear") is True and row.get("collection_within_deadline") is True
              for row in capacity["baseline_health_samples"][-3:]))
check("Профиль spike связан с неизменным исходным отчетом", spike.get("capacity_sha256") == digest(RESULTS / "cpu-capacity.json")
      and spike.get("source_commit") == SOURCE and spike.get("cpu_profile", {}).get("capacity_source_commit") == SOURCE
      and spike.get("cpu_profile", {}).get("cluster") == capacity["cluster"])
rate = spike["cpu_profile"]["total_rps"]
pair = [row for row in capacity["stages"] if row["planned_rps"] == rate]
check("Один поток измерен при двух и пяти репликах", len(pair) == 2
      and any(row["replicas"] == 2 and row["passed"] is False and row["client_p95_ms"] >= 300 for row in pair)
      and any(row["replicas"] == 5 and row["passed"] is True and row["client_p95_ms"] < 300 for row in pair))
finished = datetime.fromisoformat(capacity["finished_at"]).timestamp()
check("Калибровка завершена не более чем за 30 минут до инъекции", 0 <= spike["injected_at"] - finished <= 1800
      and spike["cpu_profile"]["capacity_finished_at"] == capacity["finished_at"])
check("Полный клиент выбранных ступеней доставил заданный поток", len(pair) == 2 and all(
    type(row["sent"]) is int and row["sent"] >= rate * capacity["duration_seconds"] * .99
    and row["dropped"] == 0 and finite(row["client"]["http_req_failed"]["rate"])
    and 0 <= row["client"]["http_req_failed"]["rate"] <= .005 for row in pair))
for row in pair:
    tail = [sample for sample in row["samples"] if sample.get("load_active") is True][-3:]
    check("Последние три активных окна подтверждают поток: " + str(row["replicas"]), len(tail) == 3
          and all(finite(sample["time"]) and finite(sample["request_rate"]) and sample["request_rate"] >= rate * .9 for sample in tail)
          and tail[-1]["time"] - tail[0]["time"] >= 20
          and all(0 < after["time"] - before["time"] <= 30 for before, after in zip(tail, tail[1:])))
    if row["replicas"] == 2:
        check("Деградация двух реплик подтверждена активными окнами", len(tail) == 3 and all(
            finite(sample["p95"]) and sample["p95"] >= .3 and finite(sample["cpu_throttle"]) and sample["cpu_throttle"] >= .4
            and finite(sample["error_ratio"]) and 0 <= sample["error_ratio"] <= .005 for sample in tail))
    elif row["replicas"] == 5:
        check("Пять реплик удержали SLO в активных окнах", len(tail) == 3 and all(
            finite(sample["p95"]) and 0 <= sample["p95"] < .3 and finite(sample["error_ratio"]) and 0 <= sample["error_ratio"] <= .005
            and sample["synthetic_success"] == 1 and sample["rollout_ready"] is True
            and finite(sample["synthetic_time"]) and 0 <= sample["time"] - sample["synthetic_time"] <= 45 for sample in tail))
check("Причины выбора всех измеренных ступеней сохранены", len(spike.get("cpu_profile_candidates", [])) == len(capacity["stages"])
      and spike["cpu_profile"].get("background_rps_during_spike") == 0)
check("CPU восстановился до конца нагрузки и масштаб снят самостоятельно", spike.get("natural_job_completion") is True
      and spike.get("autonomous_scale_down") is True and spike.get("background_restored_after_job") is True
      and spike["injected_at"] < spike["api_accepted_at"] <= spike["slo_restored_at"] < spike["scale_down_at"]
      and any(row.get("kind") == "job" and row.get("uid_confirmed") for row in spike.get("resource_intents", [])))
check("Полный клиентский итог CPU сохранен", all(key in spike.get("k6_summary", {})
      for key in ("http_reqs", "http_req_duration", "http_req_failed", "dropped_iterations", "checks")))

components = read("runtime-images.json")["components"]
check("В полном CI проверены все три обязательных рабочих компонента", set(components) >= {"checkout", "payment", "healer"})
for component, row in components.items():
    directory = ROOT / ("healer" if component in ("healer", "assistant") else "apps/" + component)
    check("Код всех рабочих реплик: " + component, row.get("passed") is True and bool(row.get("pods"))
          and all(pod.get("source_sha256") and all(digest(directory / path) == value
                  for path, value in pod["source_sha256"].items()) for pod in row["pods"]))
verifier_provenance = []
for name, script in [("limited-chaos.json", "verify-limited-chaos.py"), ("scenarios.json", "verify-scenarios.py"),
                     ("cpu-capacity.json", "capacity.py"), ("spike-lifecycle.json", "experiment.py"),
                     ("healer-observability.json", "verify-healer-observability.py")]:
    result = read(name)
    path = "scripts/" + script
    current_sha = digest(ROOT / path)
    blob_sha = hashlib.sha256(subprocess.check_output(["git", "show", SOURCE + ":" + path], cwd=ROOT)).hexdigest()
    if name == "limited-chaos.json":
        # Этот формат содержит точный SHA Git и признак чистого дерева, а не хеш утилиты.
        matched = result.get("source_commit") == SOURCE and result.get("source_worktree_dirty") is False and current_sha == blob_sha
        method = "SHA Git из исходного отчета, чистое дерево CI и точные байты файла этого коммита"
    else:
        matched = result.get("verifier_sha256") == current_sha == blob_sha
        method = "SHA-256 утилиты из исходного отчета, точные байты рабочего файла и Git"
    check("Проверочная утилита совпадает: " + script, matched)
    verifier_provenance.append({"result": name, "script": path, "method": method, "passed": matched,
                               "result_source_commit": result.get("source_commit"),
                               "result_verifier_sha256": result.get("verifier_sha256"),
                               "git_blob_sha256": blob_sha, "current_file_sha256": current_sha})

external_names = ["docs/results/iteration-6/completion-tests.json", "docs/results/iteration-6/runtime-completion.json",
                  "docs/results/iteration-6/grafana-fixed-browser.json", "docs/results/iteration-4/postmortem-current.json",
                  "docs/results/iteration-4/demo-current-video.json", "docs/results/iteration-6/source-review.json"]
external_names.append("docs/results/iteration-6/unit-current/run.json")
external_names.append("docs/results/iteration-6/unit-current/tests.json")
external_names.append("docs/results/iteration-6/unit-current/services-tests.json")
external_names.append("docs/results/iteration-6/closing-lab.json")
external_names.append("docs/results/iteration-6/commit-review.json")
external_names.extend("docs/results/iteration-6/baseline-refinement/" + name
                      for name in ("capacity.json", "reproduction.json", "environment.json"))
external = {name: json.loads((ROOT / name).read_text(encoding="utf-8")) for name in external_names}
tests = external[external_names[0]]
check("Сохраненный набор 428 тестов относится к текущему коду", tests.get("passed") is True
      and tests.get("source_commit") == SOURCE and tests.get("tests") == 428
      and all(tests.get(key) == 0 for key in ("errors", "failures", "skipped")))
check("Исходники сохраненного набора тестов совпадают с рабочей веткой", all(
    digest(ROOT / name) == value for name, value in tests["source_sha256"].items()))
unit = external["docs/results/iteration-6/unit-current/run.json"]
check("Тесты и сборка четырех заданий CI прошли на той же версии", unit.get("passed") is True
      and unit["run"]["head_sha"] == SOURCE and len(unit["jobs"]) == 4
      and all(job["conclusion"] == "success" for job in unit["jobs"]))
linux_tests = external["docs/results/iteration-6/unit-current/tests.json"]
check("Те же 428 тестов healer прошли на Linux без пропусков", linux_tests.get("passed") is True
      and linux_tests.get("source_commit") == SOURCE and linux_tests.get("package") == "healer"
      and linux_tests.get("tests_passed") == 428
      and all(linux_tests.get(key) == 0 for key in ("failures", "skipped", "errors"))
      and linux_tests.get("run_id") == unit["run"]["id"]
      and any(job["id"] == linux_tests["job_id"] and job["name"] == "healer" for job in unit["jobs"]))
service_tests = external["docs/results/iteration-6/unit-current/services-tests.json"]
check("На той же версии прошли 25 тестов checkout и 6 тестов payment", service_tests.get("passed") is True
      and service_tests.get("source_commit") == SOURCE and service_tests.get("run_id") == unit["run"]["id"]
      and set(service_tests["packages"]) == {"checkout", "payment"}
      and all(row.get("tests_passed") == expected and all(row.get(key) == 0 for key in ("failures", "skipped", "errors"))
              and any(job["id"] == row["job_id"] and job["name"] == name for job in unit["jobs"])
              for name, expected in (("checkout", 25), ("payment", 6)) for row in [service_tests["packages"][name]]))
commit_review = external["docs/results/iteration-6/commit-review.json"]
commit_rows = commit_review["first_five"] + commit_review["closing_fixes"]
check("Первые пять и десять заключительных исправлений имеют отдельные русские коммиты",
      commit_review.get("passed") is True and commit_review.get("source_commit") == SOURCE
      and len(commit_review["first_five"]) == 5 and len(commit_review["closing_fixes"]) == 10
      and len({row["commit"] for row in commit_rows}) == 15
      and all(row.get("russian_subject") is True and row.get("ancestor_of_source") is True
              and subprocess.run(["git", "merge-base", "--is-ancestor", row["commit"], SOURCE], cwd=ROOT, check=False).returncode == 0
              for row in commit_rows))
closing = external["docs/results/iteration-6/closing-lab.json"]
check("Собственный контур готов, CPU и временные воздействия возвращены", closing.get("passed") is True
      and closing.get("cluster") == "mtc-review" and closing.get("remaining_cpu_job_names") == []
      and closing.get("remaining_chaos_count") == 0 and closing.get("cpuset_equivalent") is True
      and len(closing["components"]) == 4 and all(row.get("ready") is True for row in closing["components"]))
local_series = external["docs/results/iteration-6/baseline-refinement/capacity.json"]
local_repeat = external["docs/results/iteration-6/baseline-refinement/reproduction.json"]
local_environment = external["docs/results/iteration-6/baseline-refinement/environment.json"]
check("Настоящий локальный повтор новой очистки связан с исходниками", local_repeat.get("source_commit") == SOURCE
      and local_series.get("source_commit") == SOURCE
      and local_repeat.get("capacity_returncode") == 0
      and local_repeat.get("capacity_sha256") == digest(ROOT / "docs/results/iteration-6/baseline-refinement/capacity.json")
      and all(digest(ROOT / name) == value for name, value in local_repeat["source_sha256"].items()))
check("После уточнения в локальном повторе подтверждены профиль, база и SLO", clean(local_series)
      and local_series.get("cpu_refinement", {}).get("profile_found") is True
      and local_series.get("baseline_reset", {}).get("confirmed_zero") is True
      and local_series.get("baseline_restored", {}).get("passed") is True
      and local_series.get("baseline_incident", {}).get("incident_closed") is True
      and local_repeat.get("baseline_healthy") is True
      and len(local_repeat.get("samples_after_capacity", [])) >= 3
      and all(row.get("p95", 1) < .3 and row.get("error_ratio", 1) <= .005
              and row.get("synthetic_success") == 1 and row.get("rollout_ready") is True
              and row.get("alerts_active", 1) == 0 for row in local_repeat["samples_after_capacity"][-3:]))
check("Среда после локального повтора возвращена и связана с исходным снимком", local_environment.get("passed") is True
      and local_environment.get("cpuset_equivalent") is True
      and local_repeat.get("closing_environment_sha256") == digest(ROOT / "docs/results/iteration-6/baseline-refinement/environment.json"))
local = external[external_names[1]]
check("Четыре локальных компонента подтверждены реальными подами", local.get("passed") is True
      and set(local["components"]) == {"checkout", "payment", "healer", "assistant"})
for component, row in local["components"].items():
    directory = ROOT / ("healer" if component in ("healer", "assistant") else "apps/" + component)
    check("Сохраненный локальный исходник совпадает: " + component, row.get("passed") is True
          and bool(row.get("pods")) and all(all(digest(directory / path) == value
          for path, value in pod["source_sha256"].items()) for pod in row["pods"]))
browser = external[external_names[2]]
check("Браузерная ссылка показывает оба сервиса без ручного изменения окна", browser.get("passed") is True
      and browser.get("manual_range_adjustment") is False and set(browser.get("log_services", [])) == {"checkout", "payment"})
llm = external[external_names[3]]
check("Реальная LLM-генерация не применяла действия и защищена API/RBAC", llm.get("passed") is True
      and llm.get("response", {}).get("source") == "local_llm" and llm["response"].get("actions_applied") == []
      and len(llm["authentication"]) == 4 and all(value == 401 for value in llm["authentication"].values())
      and len(llm["rbac"]) == 8 and llm["rbac"].get("get deployments") == "yes"
      and llm["rbac"].get("get heal/configmap/healer-state") == "yes"
      and all(llm["rbac"].get(key) == "no" for key in ("patch deployments", "delete pods", "get secrets",
          "patch heal/configmap/healer-state", "get heal/secrets", "patch heal/lease/healer-leader")))
video = external[external_names[4]]
check("Видео до пяти минут совпадает с проверенным файлом", video.get("passed") is True
      and video.get("encoded_duration_seconds", 301) <= 300 and video.get("decoding_passed") is True
      and video.get("visual_check_passed") is True and video.get("video_sha256") == digest(ROOT / "docs/demo-current.mp4")
      and video.get("demo_html_sha256") == digest(ROOT / "docs/demo.html"))
review = external[external_names[5]]
check("Обзор критичных исходников связан с текущей версией", review.get("runtime_commit") == SOURCE
      and review.get("confirmed_blocking_findings") == [] and len(review.get("reviewed_invariants", [])) == 11
      and all(digest(ROOT / name) == value for name, value in review.get("source_sha256", {}).items()))

summary = {"checked_at": datetime.now(timezone.utc).isoformat(), "source_commit": SOURCE,
           "auditor_sha256": digest(Path(__file__)),
           "documentation_head_before_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
           "passed": all(row["passed"] for row in checks), "checks": checks,
           "baseline_mttr_seconds": baseline["mttr_seconds"], "auto_mttr_seconds": auto["mttr_seconds"],
           "cpu_mttr_seconds": spike["mttr_seconds"], "cpu_profile_rps": rate,
           "linux_tests_passed": linux_tests["tests_passed"] + sum(row["tests_passed"] for row in service_tests["packages"].values()),
           "capacity_bounds": capacity["bounds"], "cpu_client": spike["k6_summary"],
           "verifier_provenance": verifier_provenance,
           "external_evidence_sha256": {name: digest(ROOT / name) for name in external_names},
           "artifacts_sha256": {path.name: digest(path) for path in sorted(RESULTS.glob("*.json")) if path.name != "audit.json"},
           "scope": "Независимая сверка сохраненных JSON, исходников рабочих реплик, хронологии и версии текущего полного CI. Браузер, LLM, видео и четыре локальных компонента проверены отдельно в собственном контуре, их результаты не объявляются стадиями CI. Короткие опыты не доказывают месячный SLO, промышленный максимум или причинное превосходство автоматизации."}
(RESULTS / "audit.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
print(json.dumps({key: summary[key] for key in ("passed", "baseline_mttr_seconds", "auto_mttr_seconds", "cpu_mttr_seconds", "cpu_profile_rps", "capacity_bounds")}, ensure_ascii=False))
print(json.dumps({"checks": len(checks), "failed": [row["name"] for row in checks if not row["passed"]]}, ensure_ascii=False))
if not summary["passed"]:
    raise SystemExit(1)
