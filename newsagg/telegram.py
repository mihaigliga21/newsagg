"""Minimal Telegram Bot API client (sendMessage, edit, callbacks, long polling)."""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    pass


class TelegramBot:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}"
        self.http = httpx.AsyncClient(timeout=70)

    async def _call(self, method: str, **params):
        resp = await self.http.post(f"{self.base}/{method}", json={k: v for k, v in params.items() if v is not None})
        data = resp.json()
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description')}")
        return data["result"]

    async def send_message(self, chat_id: str, text: str, buttons: list[list[dict]] | None = None) -> int:
        result = await self._call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            link_preview_options={"is_disabled": True},
            reply_markup={"inline_keyboard": buttons} if buttons else None,
        )
        return result["message_id"]

    async def edit_message_text(self, chat_id: str, message_id: int, text: str):
        await self._call(
            "editMessageText", chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML",
            link_preview_options={"is_disabled": True},
        )

    async def answer_callback(self, callback_id: str, text: str | None = None):
        await self._call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    async def get_updates(self, offset: int | None, timeout: int = 50) -> list[dict]:
        return await self._call(
            "getUpdates", offset=offset, timeout=timeout, allowed_updates=["message", "callback_query"]
        )

    async def close(self):
        await self.http.aclose()
