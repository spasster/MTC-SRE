from app.telegram import parse_updates


def test_parse_updates_keeps_our_chat_only():
    body = {
        "result": [
            {
                "update_id": 4,
                "callback_query": {
                    "id": "cb",
                    "data": "approve",
                    "message": {"chat": {"id": 77}},
                },
            },
            {
                "update_id": 5,
                "callback_query": {
                    "id": "other",
                    "data": "reject",
                    "message": {"chat": {"id": 8}},
                },
            },
        ]
    }
    offset, found = parse_updates(body, "77")
    assert offset == 6
    assert found == [("cb", "approve")]
