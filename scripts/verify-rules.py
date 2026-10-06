"""Проверить синтаксис всех правил и расход бюджета через promtool установленной версии."""

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", help="Образ Prometheus; по умолчанию берется из тестового кластера")
    parser.add_argument("--output", help="Сохранить результат проверки")
    args = parser.parse_args()
    image = args.image
    if not image:
        response = subprocess.run(["kubectl", "get", "statefulset", "prometheus-kube-prom-kube-prometheus-prometheus", "-n", "obs", "-o", "json"], capture_output=True, text=True, check=True)
        image = json.loads(response.stdout)["spec"]["template"]["spec"]["containers"][0]["image"]
    with tempfile.TemporaryDirectory(prefix="mtc-promtool-") as temp:
        directory = Path(temp)
        directory.chmod(0o755)
        names = []
        for file in ("rules.yaml", "healer-rules.yaml", "slo-budget.yaml"):
            name = file.replace(".yaml", "-rules.yml")
            body = yaml.safe_load((Path("observability") / file).read_text(encoding="utf-8"))
            (directory / name).write_text(yaml.safe_dump(body["spec"], allow_unicode=True), encoding="utf-8")
            names.append(name)
        (directory / "slo-budget.yml").write_text(Path("observability/tests/slo-budget.yml").read_text(encoding="utf-8"), encoding="utf-8")
        base = ["docker", "run", "--rm", "--entrypoint", "/bin/promtool", "-v", str(directory) + ":/tests:ro", "-w", "/tests", image]
        subprocess.run(base + ["check", "rules", *names], check=True)
        subprocess.run(base + ["test", "rules", "slo-budget.yml"], check=True)
    if args.output:
        file = Path(args.output)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps({"passed": True, "image": image, "syntax_checked": names,
                                    "tests": ["fast_burn_after_full_window", "slow_burn_after_full_window",
                                              "insufficient_history", "missing_data", "short_window_recovery"]}, indent=2) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
