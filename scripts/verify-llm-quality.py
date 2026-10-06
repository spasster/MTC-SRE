"""Проверить настоящую модель на сохраненных фактах нескольких реальных инцидентов."""

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from experiment import cmd

EVALUATE = '''
import json,sys,time
from app.assistant import build_report, factual_sections
case=json.load(sys.stdin)
started=time.time()
try:
    response=build_report(case["evidence"],incident=case["incident_facts"])
    fixed=factual_sections(response["slo_assessment"],case["incident_facts"])
    facts_preserved=all(response["report_sections"][key]==value for key,value in fixed.items())
    selected=response["selected_recommendation"]
    passed=(facts_preserved and selected["group"]==case["expected_group"]
            and selected["text"]==response["report_sections"]["next_checks"]
            and selected["basis"]["source"]=="selected_incident" and response["actions_applied"]==[])
    print(json.dumps({"passed":passed,"duration_seconds":time.time()-started,
                      "facts_preserved":facts_preserved,"response":response},ensure_ascii=False))
except Exception as error:
    print(json.dumps({"passed":False,"duration_seconds":time.time()-started,
                      "error_type":type(error).__name__},ensure_ascii=False))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default="mtc")
    parser.add_argument("--dataset", default="docs/results/iteration-2/llm-cases.json")
    parser.add_argument("--output", default="docs/results/llm-quality.json")
    args = parser.parse_args()
    cmd("sh", "scripts/require-context.sh", args.cluster)
    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    cases = dataset.get("cases") if isinstance(dataset, dict) else None
    if not isinstance(cases, list) or len(cases) < 3:
        raise SystemExit("Для оценки нужны не менее трех сохраненных инцидентов")
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "passed": False, "cases": [],
              "source_commit": cmd("git", "rev-parse", "HEAD").strip(),
              "source_worktree_dirty": bool(cmd("git", "status", "--porcelain")),
              "method": "Последовательная генерация настоящего Ollama в поде ассистента по сохраненным фактам реальных инцидентов. Это повтор на зафиксированном наборе, а не новые живые инциденты.",
              "criteria": ["Факты и MTTR сохранены", "Конкретная проверка связана с выбранным инцидентом", "Посторонний компонент не добавлен", "Действия не применены"],
              "limitation": "Проверен ограниченный выбор обоснованной проверки из явных кандидатов. Свободный поиск первопричины и полезность вне набора не доказаны; отчет проверяет человек."}
    for case in cases:
        try:
            completed = subprocess.run(["kubectl", "exec", "-i", "-n", "heal", "deployment/sre-assistant", "-c", "assistant",
                                        "--", "python", "-c", EVALUATE], input=json.dumps(case), text=True,
                                       capture_output=True, timeout=180)
            result = json.loads(completed.stdout) if completed.returncode == 0 else {"passed": False, "error_type": "Процесс генерации завершился ошибкой"}
        except subprocess.TimeoutExpired:
            result = {"passed": False, "error_type": "Превышено время генерации"}
        except ValueError:
            result = {"passed": False, "error_type": "Ответ процесса не является JSON"}
        result.update(case=case["id"], proof_source=case["proof_source"], expected_group=case["expected_group"])
        report["cases"].append(result)
        print(f"LLM, пример {case['id']}: результат={result['passed']}", flush=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    report["passed"] = len(report["cases"]) >= 3 and all(case["passed"] for case in report["cases"])
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    if not report["passed"]:
        raise SystemExit("Качество ограниченных рекомендаций не подтверждено по всем примерам")


if __name__ == "__main__":
    main()
