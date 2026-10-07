"""Выбрать CPU-профиль по завершенным замерам того же контура и версии."""
import math
from datetime import datetime, timezone


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def healthy_sample(sample):
    return (finite(sample.get("p95")) and 0 <= sample["p95"] < .3
            and finite(sample.get("error_ratio")) and 0 <= sample["error_ratio"] <= .005
            and finite(sample.get("request_rate")) and sample["request_rate"] > 0
            and sample.get("synthetic_success") == 1 and sample.get("rollout_ready") is True
            and finite(sample.get("time")) and finite(sample.get("synthetic_time"))
            and 0 <= sample["time"] - sample["synthetic_time"] <= 45)


def selected_profile(report, cluster, commit, now=None):
    if (not isinstance(report, dict) or report.get("passed") is not True or report.get("cleanup_errors") != []
            or report.get("cluster") != cluster or report.get("source_commit") != commit):
        raise ValueError("Замеры мощности не подтверждены для текущего контура и версии")
    try:
        timestamp = datetime.fromisoformat(report["finished_at"])
        if timestamp.tzinfo is None:
            raise ValueError("Нет часового пояса")
        ended = timestamp.timestamp()
    except (KeyError, TypeError, ValueError):
        raise ValueError("Замеры мощности не имеют подтвержденного времени завершения") from None
    age = (datetime.now(timezone.utc).timestamp() if now is None else now) - ended
    duration = report.get("duration_seconds")
    if not 0 <= age <= 1800 or type(duration) is not int or duration < 90:
        raise ValueError("Нужны свежие завершенные ступени длительностью не менее 90 секунд")
    stages = report.get("stages", [])
    failing_two = [row for row in stages if row.get("replicas") == 2 and row.get("passed") is False
                   and len(row.get("samples", [])) >= 3
                   and all(finite(sample.get("p95")) and sample["p95"] >= .3
                           and finite(sample.get("cpu_throttle")) and sample["cpu_throttle"] >= .4
                           and finite(sample.get("error_ratio")) and 0 <= sample["error_ratio"] <= .005
                           and finite(sample.get("request_rate")) and sample["request_rate"] > 0
                           for sample in row["samples"][-3:])]
    for failed in failing_two:
        rate = failed.get("planned_rps")
        if type(rate) is not int or rate <= 0:
            continue
        for healthy in stages:
            client = healthy.get("client", {})
            if (healthy.get("replicas") == 5 and healthy.get("planned_rps") == rate and healthy.get("passed") is True
                    and finite(healthy.get("client_p95_ms")) and 0 <= healthy["client_p95_ms"] < 300
                    and healthy.get("dropped") == 0 and type(healthy.get("sent")) is int
                    and healthy["sent"] >= rate * duration * .99
                    and finite(client.get("http_req_failed", {}).get("rate"))
                    and 0 <= client["http_req_failed"]["rate"] <= .005
                    and len(healthy.get("samples", [])) >= 3 and all(healthy_sample(row) for row in healthy["samples"][-3:])):
                return {"total_rps": rate, "background_rps_during_spike": 0,
                        "capacity_source_commit": commit, "cluster": cluster,
                        "capacity_finished_at": report["finished_at"],
                        "two_replicas_client_p95_ms": failed.get("client_p95_ms"),
                        "five_replicas_client_p95_ms": healthy["client_p95_ms"],
                        "method": "Один и тот же поток проверен при двух и пяти репликах. В CPU-опыте фон временно выключен, весь поток создает собственный Job; эффект scale подтверждается отдельно до окончания нагрузки."}
    raise ValueError("Не найден измеренный CPU-профиль: две реплики нарушают SLO, пять выдерживают тот же поток")
