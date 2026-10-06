import os
import re

import httpx


class TelegramError(Exception):
    """Ошибка Telegram без URL, содержащего токен бота."""


def parse_updates(body, chat_id):
    found = []
    offset = None
    for update in body.get("result") or []:
        offset = int(update["update_id"]) + 1
        query = update.get("callback_query") or {}
        message = query.get("message") or {}
        chat = (message.get("chat") or {}).get("id")
        data = query.get("data")
        if (
            str(chat) == str(chat_id)
            and isinstance(data, str)
            and re.fullmatch(r"(?:approve|reject):[0-9a-f]{32}", data)
        ):
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

    def _call(self, method: str, **kwargs):
        try:
            response = self.http.request(
                "GET" if method == "getUpdates" else "POST", self._url(method), **kwargs,
            )
            if not response.is_success:
                raise TelegramError(f"Telegram {method}: HTTP {response.status_code}")
            body = response.json()
            if not isinstance(body, dict) or body.get("ok") is not True:
                raise TelegramError(f"Telegram {method}: API отклонил запрос")
            return body
        except httpx.HTTPError:
            raise TelegramError(f"Telegram {method}: ошибка соединения") from None
        except ValueError:
            raise TelegramError(f"Telegram {method}: некорректный ответ") from None

    def send(self, text: str, buttons: bool = False, approval_id: str = "") -> None:
        payload = {"chat_id": self.chat_id, "text": text}
        if buttons:
            if not re.fullmatch(r"[0-9a-f]{32}", approval_id):
                raise ValueError("Для кнопок требуется идентификатор запроса подтверждения")
            payload["reply_markup"] = {
                "inline_keyboard": [[
                    {"text": "✅ Разрешить", "callback_data": f"approve:{approval_id}"},
                    {"text": "🚫 Отклонить", "callback_data": f"reject:{approval_id}"},
                ]]
            }
        self._call("sendMessage", json=payload)

    def answer(self, callback_id: str) -> None:
        if not callback_id:
            return
        self._call("answerCallbackQuery", json={"callback_query_id": callback_id})

    def poll(self, timeout: int = 25):
        params = {"timeout": timeout}
        if self.offset is not None:
            params["offset"] = self.offset
        body = self._call("getUpdates", params=params)
        offset, found = parse_updates(body, self.chat_id)
        if offset is not None:
            self.offset = offset
        return found
