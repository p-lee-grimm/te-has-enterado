"""Telegram Bot API. Обычный sendMessage с HTML-разметкой (§1)."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .config import env

log = logging.getLogger(__name__)

API = "https://api.telegram.org"


class TelegramError(RuntimeError):
    pass


def _call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    token = env("TELEGRAM_BOT_TOKEN", required=True)
    resp = httpx.post(f"{API}/bot{token}/{method}", json=payload, timeout=60)
    data = resp.json() if resp.content else {}
    if not data.get("ok"):
        raise TelegramError(f"{method}: {data.get('description', resp.text[:200])}")
    return data["result"]


def send_message(
    chat_id: str,
    text: str,
    *,
    reply_markup: dict | None = None,
    disable_preview: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": disable_preview},
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return _call("sendMessage", payload)


def notify_owner(text: str) -> None:
    """Личное сообщение владельцу. Используется, когда выпуск не вышел (§3.10)."""
    chat = env("TELEGRAM_OWNER_CHAT_ID")
    if not chat:
        log.warning("TELEGRAM_OWNER_CHAT_ID не задан — не могу сообщить владельцу: %s", text[:200])
        return
    try:
        send_message(chat, text)
    except TelegramError as exc:
        log.error("Не удалось уведомить владельца: %s", exc)


def review_keyboard(digest_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [[
            {"text": "✅ Опубликовать", "callback_data": f"pub:{digest_id}"},
            {"text": "⏭ Пропустить", "callback_data": f"skip:{digest_id}"},
        ]]
    }


def get_updates(offset: int | None = None, timeout: int = 30) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["callback_query"]}
    if offset is not None:
        payload["offset"] = offset
    return _call("getUpdates", payload)


def answer_callback(callback_id: str, text: str) -> None:
    try:
        _call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
    except TelegramError as exc:
        log.warning("answerCallbackQuery: %s", exc)


def edit_reply_markup(chat_id: str, message_id: int) -> None:
    """Убирает кнопки после нажатия, чтобы не жали дважды."""
    try:
        _call("editMessageReplyMarkup", {
            "chat_id": chat_id, "message_id": message_id,
            "reply_markup": json.dumps({"inline_keyboard": []}),
        })
    except TelegramError as exc:
        log.debug("editMessageReplyMarkup: %s", exc)
