"""Telegram Bot API. Обычный sendMessage с HTML-разметкой (§1)."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from .config import env

log = logging.getLogger(__name__)

API = "https://api.telegram.org"


class TelegramError(RuntimeError):
    pass


# Пауз ждём столько, сколько скажет Telegram, но не бесконечно: правка
# сорока постов подряд упирается в лимит регулярно, а зависший на десять
# минут прогон хуже, чем недоправленный пост.
MAX_RETRY_WAIT = 60
RETRIES_ON_LIMIT = 2


def _call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Вызов метода API. На 429 ждёт столько, сколько просит Telegram.

    Без этого массовая правка теряет посты пачками: лимит на редактирование
    в канале выбирается за пару десятков сообщений, и весь остаток прогона
    получает «Too Many Requests».
    """
    token = env("TELEGRAM_BOT_TOKEN", required=True)

    for attempt in range(RETRIES_ON_LIMIT + 1):
        resp = httpx.post(f"{API}/bot{token}/{method}", json=payload, timeout=60)
        data = resp.json() if resp.content else {}
        if data.get("ok"):
            return data["result"]

        wait = int((data.get("parameters") or {}).get("retry_after", 0))
        if wait and attempt < RETRIES_ON_LIMIT and wait <= MAX_RETRY_WAIT:
            log.info("%s: лимит Telegram, жду %s с", method, wait)
            time.sleep(wait + 1)
            continue

        raise TelegramError(f"{method}: {data.get('description', resp.text[:200])}")

    raise TelegramError(f"{method}: лимит не отпустил за {RETRIES_ON_LIMIT} попытки")


def send_message(
    chat_id: str,
    text: str,
    *,
    reply_markup: dict | None = None,
    disable_preview: bool = True,
    silent: bool = False,
    reply_to_message_id: int | None = None,
) -> dict[str, Any]:
    """silent=True — пост приходит без звука и без вибрации.

    Правка уже опубликованного сообщения уведомление не шлёт никогда, так что
    дополнение поста новыми изданиями читателя не беспокоит.
    """
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_notification": silent,
        "link_preview_options": {"is_disabled": disable_preview},
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if reply_to_message_id:
        # allow_sending_without_reply: если исходное сообщение удалено,
        # продолжение всё равно уходит, а не теряется
        payload["reply_parameters"] = {
            "message_id": reply_to_message_id,
            "allow_sending_without_reply": True,
        }
    return _call("sendMessage", payload)


def edit_message_text(
    chat_id: str, message_id: int, text: str, *, disable_preview: bool = True
) -> dict[str, Any]:
    """Правка уже опубликованного поста (§ дополнение по мере выхода изданий)."""
    return _call("editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": disable_preview},
    })


def delete_message(chat_id: str, message_id: int) -> None:
    try:
        _call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
    except TelegramError as exc:
        log.warning("deleteMessage: %s", exc)


def channel_username() -> str:
    """Имя канала для ссылок. Отдельно от id: id стабилен, имя — нет."""
    return env("TELEGRAM_CHANNEL_USERNAME", "").lstrip("@")


def message_link(message_id: int, chat_username: str | None = None) -> str:
    name = (chat_username or channel_username()).lstrip("@")
    return f"https://t.me/{name}/{message_id}" if name else ""


def review_chat_id() -> str:
    """Чат служебных сообщений. REVIEW_CHAT_ID, иначе личка владельца."""
    return env("REVIEW_CHAT_ID") or env("TELEGRAM_OWNER_CHAT_ID")


def notify_owner(text: str, *, reply_markup: dict | None = None) -> dict | None:
    """Служебное сообщение в чат ревью.

    Единственный канал: аварии, ворота, карточки, черновики, сводки. Соблазн
    развести это по разным местам возникает быстро; одно место просматривают,
    три игнорируют (§9).
    """
    chat = review_chat_id()
    if not chat:
        log.warning("REVIEW_CHAT_ID не задан — некуда написать: %s", text[:200])
        return None
    try:
        # служебные сообщения всегда беззвучно: это рабочая переписка, не новость
        return send_message(chat, text, reply_markup=reply_markup, silent=True)
    except TelegramError as exc:
        log.error("Не удалось написать в чат ревью: %s", exc)
        return None


def review_keyboard(digest_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [[
            {"text": "✅ Опубликовать", "callback_data": f"pub:{digest_id}"},
            {"text": "⏭ Пропустить", "callback_data": f"skip:{digest_id}"},
        ]]
    }


# allowed_updates у Telegram — липкий и ИСКЛЮЧАЮЩИЙ: всё, чего нет в списке,
# отбрасывается и не доставляется никогда. Нужны оба типа: кнопки приходят
# callback_query, а правка карточки реплаем — обычным message.
ALLOWED_UPDATES = ["callback_query", "message"]


def get_updates(offset: int | None = None, timeout: int = 30) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ALLOWED_UPDATES}
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
