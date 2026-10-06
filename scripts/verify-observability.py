"""Проверка одного реального перехода метрика -> трейс -> лог."""

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen


def get(url, params=None):
    with urlopen(url + ("?" + urlencode(params) if params else ""), timeout=15) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus", default="http://127.0.0.1:19090")
    parser.add_argument("--tempo", default="http://127.0.0.1:13200")
    parser.add_argument("--loki", default="http://127.0.0.1:13100")
    parser.add_argument("--output", default=".runtime/observability.json")
    parser.add_argument("--since", type=float)
    parser.add_argument("--min-duration", type=float, default=0)
    args = parser.parse_args()
    now = time.time()
    result = get(args.prometheus + "/api/v1/query_exemplars", {
        "query": 'http_request_duration_seconds_bucket{namespace="demo",job="checkout",path="/work"}',
        "start": max(now - 120, args.since or 0), "end": now - 5,
    })
    traces = {}
    for series in result.get("data", []):
        for sample in series.get("exemplars", []):
            trace_id = sample.get("labels", {}).get("trace_id")
            if trace_id and float(sample["value"]) >= args.min_duration and sample["timestamp"] >= (args.since or 0):
                traces[trace_id] = {"duration_seconds": float(sample["value"]), "request_at": sample["timestamp"]}
    for trace_id in sorted(traces):
        try:
            trace = get(args.tempo + "/api/traces/" + trace_id)
            encoded = json.dumps(trace)
            if "checkout" not in encoded or "payment" not in encoded:
                continue
            logs = get(args.loki + "/loki/api/v1/query_range", {
                "query": '{namespace="demo"} | json | trace_id="' + trace_id + '"',
                "start": int((now - 120) * 1e9), "end": int(now * 1e9), "limit": 20,
            })
            rows = logs.get("data", {}).get("result", [])
            apps = {row.get("stream", {}).get("app") for row in rows}
            if not {"checkout", "payment"}.issubset(apps):
                continue
            report = {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "passed": True, "trace_id": trace_id,
                "metric": "http_request_duration_seconds_bucket",
                "metric_exemplar_found": True,
                "trace_services": ["checkout", "payment"],
                "log_services": ["checkout", "payment"],
                "log_entries": sum(len(row.get("values", [])) for row in rows),
                "incident_since": args.since,
                "request": traces[trace_id],
            }
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
            print("Проверен реальный exemplar, распределённый трейс и логи обоих сервисов.")
            return
        except Exception:
            continue
    raise SystemExit("Не найден полный переход метрика -> трейс -> лог за последние две минуты")


if __name__ == "__main__":
    main()
