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
                    "from": {"id": 77, "is_bot": False},
                    "data": f"approve:{APPROVAL}",
                    "message": {"chat": {"id": 77}},
                },
            },
            {
                "update_id": 5,
                "callback_query": {
                    "id": "other",
                    "from": {"id": 77, "is_bot": False},
                    "data": f"reject:{APPROVAL}",
                    "message": {"chat": {"id": 8}},
                },
            },
        ]
    }
    offset, found = parse_updates(body, "77")
    assert offset == 6
    assert found == [("cb", f"approve:{APPROVAL}", "77")]


@pytest.mark.parametrize("sender", [{}, {"id": 77}, {"id": 77, "is_bot": True}, {"id": True, "is_bot": False}])
def test_missing_or_bot_sender_cannot_supply_a_callback(sender):
    body = {"result": [{"update_id": 1, "callback_query": {
        "id": "cb", "from": sender, "data": f"approve:{APPROVAL}", "message": {"chat": {"id": 77}},
    }}]}
    assert parse_updates(body, "77") == (2, [])


def test_operator_list_is_required_and_does_not_authorize_other_members(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_IDS", raising=False)
    assert not Telegram(token="test", chat_id="77").authorized("77")
    bot = Telegram(token="test", chat_id="77", allowed_user_ids="77,88,invalid,-1")
    assert bot.authorized(77) and bot.authorized("88")
    assert not bot.authorized("99")


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
