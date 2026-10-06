"""Проверить доставку контрольных событий rsyslog и auditd в Loki."""

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen


def count(base, query):
    params = {"query": query, "start": int((time.time() - 600) * 1e9), "limit": 1000}
    with urlopen(base + "/loki/api/v1/query_range?" + urlencode(params), timeout=10) as response:
        result = json.load(response)
    return sum(len(row.get("values", [])) for row in result.get("data", {}).get("result", []))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loki", default="http://127.0.0.1:13100")
    parser.add_argument("--host", default="linux")
    parser.add_argument("--output", default=".runtime/node-logs.json")
    args = parser.parse_args()
    for _ in range(24):
        syslog = count(args.loki, '{job="syslog",host="' + args.host + '"} |= "mtc-sre"')
        audit = count(args.loki, '{job="audit",host="' + args.host + '"} |= "mtc-marker"')
        if syslog and audit:
            result = {"checked_at": datetime.now(timezone.utc).isoformat(), "passed": True,
                      "syslog_marker_entries": syslog, "audit_marker_entries": audit}
            target = Path(args.output)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            print("Проверена доставка контрольных событий rsyslog и auditd в Loki.")
            return
        time.sleep(5)
    raise SystemExit("Контрольные события обоих журналов не найдены; доставка не подтверждена")


if __name__ == "__main__":
    main()
