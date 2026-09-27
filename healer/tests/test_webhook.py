import json
import logging

from fastapi.testclient import TestClient

from app import main
from app.main import JsonFormatter, app

client = TestClient(app)


class FakeActuator:
    def __init__(self):
        self.calls = []

    def scale(self, namespace, name, replicas):
        self.calls.append(("scale", namespace, name, replicas))
        return f"scale replicas={replicas}"

    def restart(self, namespace, name):
        self.calls.append(("restart", namespace, name))
        return "restart"

    def rollback(self, namespace, name):
        self.calls.append(("rollback", namespace, name))
        return "rollback to revision 2"


def setup_function():
    main.reset_state()
    main.actuator = FakeActuator()


def alert(name, action="restart", status="firing", service="checkout"):
    return {
        "status": status,
        "labels": {"alertname": name, "action": action, "service": service},
    }


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_restart_firing():
    response = client.post("/webhook", json={"alerts": [alert("CheckoutHighErrorRate", "restart")]})
    assert response.status_code == 200
    assert response.json()["results"][0]["result"] == "restart"
    assert main.actuator.calls == [("restart", "demo", "checkout")]


def test_scale():
    client.post("/webhook", json={"alerts": [alert("CheckoutSlow", "scale")]})
    assert main.actuator.calls == [("scale", "demo", "checkout", 5)]


def test_rollback():
    client.post("/webhook", json={"alerts": [alert("CheckoutHighErrorRate", "rollback")]})
    assert main.actuator.calls == [("rollback", "demo", "checkout")]


def test_ignore_resolved_and_unknown():
    response = client.post(
        "/webhook",
        json={
            "alerts": [
                alert("CheckoutHighErrorRate", status="resolved"),
                alert("RedisDown", "fix-redis"),
                alert("CheckoutSlow", "scale", service="obs"),
            ]
        },
    )
    assert main.actuator.calls == []
    results = [item["result"] for item in response.json()["results"]]
    assert results == [
        "ignored: not firing",
        "ignored: not in whitelist",
        "ignored: deployment not allowed",
    ]


def test_cooldown():
    client.post("/webhook", json={"alerts": [alert("CheckoutHighErrorRate")]})
    client.post("/webhook", json={"alerts": [alert("CheckoutHighErrorRate")]})
    assert len(main.actuator.calls) == 1


def test_log_line(caplog):
    with caplog.at_level(logging.INFO):
        client.post("/webhook", json={"alerts": [alert("CheckoutSlow", "scale")]})
    record = next(item for item in caplog.records if getattr(item, "alertname", None) == "CheckoutSlow")
    payload = json.loads(JsonFormatter().format(record))
    assert payload["action"] == "scale"
    assert payload["result"] == "scale replicas=5"
