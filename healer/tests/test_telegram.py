import traceback

import httpx
import pytest

from app.telegram import Telegram, TelegramError, parse_updates

APPROVAL = "a" * 32


def test_parse_updates_keeps_our_chat_only():
    body = {
        "result": [
            {
                "update_id": 4,
                "callback_query": {
                    "id": "cb",
                    "data": f"approve:{APPROVAL}",
                    "message": {"chat": {"id": 77}},
                },
            },
            {
                "update_id": 5,
                "callback_query": {
                    "id": "other",
                    "data": f"reject:{APPROVAL}",
                    "message": {"chat": {"id": 8}},
                },
            },
        ]
    }
    offset, found = parse_updates(body, "77")
    assert offset == 6
    assert found == [("cb", f"approve:{APPROVAL}")]


def test_plain_or_malformed_callbacks_are_ignored():
    body = {"result": [
        {"update_id": index, "callback_query": {"id": str(index), "data": data, "message": {"chat": {"id": 77}}}}
        for index, data in enumerate(["approve", "reject", "approve:old", None], 1)
    ]}
    offset, found = parse_updates(body, "77")
    assert offset == 5
    assert found == []


@pytest.mark.parametrize("method", ["send", "answer", "poll"])
@pytest.mark.parametrize("failure", ["http", "network", "api", "json"])
def test_api_errors_never_expose_the_token(method, failure):
    token = "test-secret-never-log"

    def respond(request):
        if failure == "network":
            raise httpx.ConnectError(str(request.url), request=request)
        if failure == "json":
            return httpx.Response(200, text="invalid")
        return httpx.Response(500 if failure == "http" else 200,
                              json={"ok": False, "description": str(request.url)})

    bot = Telegram(token=token, chat_id="77", client=httpx.Client(transport=httpx.MockTransport(respond)))
    with pytest.raises(TelegramError) as caught:
        getattr(bot, method)("test" if method != "poll" else 0)
    formatted = "".join(traceback.format_exception(caught.value))
    assert token not in formatted
    assert "api.telegram.org" not in formatted


def test_poll_records_offset_after_a_successful_response():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "ok": True, "result": [{"update_id": 8}],
    }))
    bot = Telegram(token="test", chat_id="77", client=httpx.Client(transport=transport))
    assert bot.poll(timeout=0) == []
    assert bot.offset == 9
