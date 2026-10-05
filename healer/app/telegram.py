import os

import httpx


def parse_updates(body, chat_id):
    found = []
    offset = None
    for update in body.get("result") or []:
        offset = int(update["update_id"]) + 1
        query = update.get("callback_query") or {}
        message = query.get("message") or {}
        chat = (message.get("chat") or {}).get("id")
        data = query.get("data")
        if str(chat) == str(chat_id) and data in {"approve", "reject"}:
            found.append((query.get("id"), data))
    return offset, found


class Telegram:
    def __init__(self, token="", chat_id="", client=None):
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = str(chat_id or os.getenv("TELEGRAM_CHAT_ID", ""))
        self.offset = None
        self.http = client or httpx.Client(timeout=35)

    def enabled(self) -> bool:
        return bool(self.token and self.chat_id and self.chat_id != "0")

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def send(self, text: str, buttons: bool = False) -> None:
        payload = {"chat_id": self.chat_id, "text": text}
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [[
                    {"text": "✅ Разрешить", "callback_data": "approve"},
                    {"text": "🚫 Отклонить", "callback_data": "reject"},
                ]]
            }
        response = self.http.post(self._url("sendMessage"), json=payload)
        response.raise_for_status()

    def answer(self, callback_id: str) -> None:
        if not callback_id:
            return
        response = self.http.post(
            self._url("answerCallbackQuery"),
            json={"callback_query_id": callback_id},
        )
        response.raise_for_status()

    def poll(self, timeout: int = 25):
        params = {"timeout": timeout}
        if self.offset is not None:
            params["offset"] = self.offset
        response = self.http.get(self._url("getUpdates"), params=params)
        response.raise_for_status()
        offset, found = parse_updates(response.json(), self.chat_id)
        if offset is not None:
            self.offset = offset
        return found
