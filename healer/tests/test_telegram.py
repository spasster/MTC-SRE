from app.telegram import parse_updates

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
