class Decision:
    def __init__(self, diagnosis, mode, action, target, detail):
        self.diagnosis = diagnosis
        self.mode = mode
        self.action = action
        self.target = target
        self.detail = detail

    def as_dict(self):
        return {
            "diagnosis": self.diagnosis,
            "mode": self.mode,
            "action": self.action,
            "target": self.target,
            "detail": self.detail,
        }


def _ratio(value):
    if value is None:
        return 0.0
    return value


def _pods(evidence):
    return [pod for pod in evidence.get("pods") or [] if pod.get("p95") is not None]


def _recent(pod):
    if "p95_recent" in pod:
        return pod.get("p95_recent")
    return pod.get("p95")


def _live(pods):
    if pods and all("p95_recent" in pod for pod in pods):
        return [pod for pod in pods if pod.get("p95_recent") is not None]
    return pods


def decide(evidence, rules):
    if not evidence or evidence.get("error"):
        return Decision("unknown", "escalate", "none", None, "Не смог собрать данные и ничего не менял.")

    quiet = rules["dependency_ratio"]
    revision_age = evidence.get("revision_age_seconds")
    error_ratio = _ratio(evidence.get("error_ratio"))
    redis_ratio = _ratio(evidence.get("redis_error_ratio"))
    payment_timeout = _ratio(evidence.get("payment_timeout_ratio"))
    fresh = revision_age is not None and revision_age <= rules["revision_window_seconds"]
    if fresh and error_ratio > rules["error_ratio"] and redis_ratio < quiet and payment_timeout < quiet:
        return Decision(
            "bad_release",
            "approval",
            "rollback",
            "checkout",
            "Ревизия checkout моложе 10 минут, 5xx пошли сразу, Redis и payment тихие.",
        )

    if redis_ratio >= quiet and evidence.get("redis_ready"):
        return Decision(
            "redis",
            "approval",
            "restart_redis",
            "redis",
            "Redis Ready, но checkout ловит таймаут. Рестарт сотрёт корзину: она только в памяти.",
        )

    if payment_timeout >= quiet and evidence.get("payment_ready"):
        return Decision(
            "partition",
            "escalate",
            "none",
            "payment",
            "payment Ready, у checkout до него таймаут. Delete пода и scale сеть не откроют.",
        )

    pods = _live(_pods(evidence))
    slow_ones = [pod for pod in pods if pod["p95"] >= rules["slow_seconds"]]
    if len(pods) >= 2 and len(slow_ones) == 1:
        worst = slow_ones[0]
        best = min(pods, key=lambda pod: pod["p95"])
        baseline = best["p95"] if best["p95"] > 0 else 0.001
        slower = worst["p95"] >= baseline * rules["slow_ratio"]
        throttle = worst.get("throttle") or 0.0
        recent = _recent(worst)
        if slower and throttle < rules["cpu_throttle"] and recent is not None and recent >= rules["slow_seconds"]:
            return Decision(
                "slow_pod",
                "auto",
                "delete_pod",
                worst["pod"],
                "Один под минимум в 3 раза медленнее остальных, throttle CPU ниже 0.4.",
            )

    if pods and all(_recent(pod) is not None and _recent(pod) < rules["slow_seconds"] for pod in pods):
        return Decision(
            "settling",
            "ignore",
            "none",
            None,
            "за последние 30 секунд поды уже быстрые",
        )

    replicas = evidence.get("replicas") or 0
    aggregate = evidence.get("p95")
    if (
        replicas >= rules["scale_to"]
        and aggregate is not None
        and aggregate >= rules["slow_seconds"]
    ):
        return Decision(
            "cpu",
            "escalate",
            "none",
            "checkout",
            "Реплик checkout уже 5, а p95 GET /work всё ещё выше 300 мс.",
        )

    throttled = [pod for pod in pods if (pod.get("throttle") or 0.0) >= rules["cpu_throttle"]]
    slow = [pod for pod in pods if pod["p95"] >= rules["slow_seconds"]]
    if pods and len(throttled) == len(pods) and slow:
        if replicas >= rules["scale_to"]:
            return Decision(
                "cpu",
                "escalate",
                "none",
                "checkout",
                "Реплик checkout уже 5, это потолок.",
            )
        return Decision(
            "cpu",
            "auto",
            "scale",
            str(rules["scale_to"]),
            "Все поды медленные, throttle CPU не ниже 0.4.",
        )

    return Decision("unknown", "escalate", "none", None, "Похоже на поломку, но ни одна знакомая картина не подошла.")
