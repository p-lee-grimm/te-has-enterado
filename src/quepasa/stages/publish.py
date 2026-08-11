"""Стадия publish (§3.11).

REVIEW_MODE=true  — пост уходит владельцу в личку с кнопками, публикуется
                   только по нажатию; без нажатия за N часов не публикуется.
REVIEW_MODE=false — публикуем сразу в канал.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..config import env, get_settings
from ..db import connect
from ..telegram import (
    TelegramError, answer_callback, edit_reply_markup, get_updates,
    notify_owner, review_keyboard, send_message,
)

log = logging.getLogger(__name__)


def save_digest(conn, items: list[dict[str, Any]], messages: list[str], status: str,
                gate_report: dict | None) -> int:
    import json as _json

    digest_id = conn.execute(
        """
        INSERT INTO digests (status, item_count, gate_report, body_html)
        VALUES (%s, %s, %s, %s) RETURNING id
        """,
        (status, len(items), _json.dumps(gate_report or {}, ensure_ascii=False),
         "\n\n".join(messages)),
    ).fetchone()["id"]

    for pos, item in enumerate(items, 1):
        conn.execute(
            """
            INSERT INTO digest_items
                (digest_id, cluster_id, position, headline, summary, context,
                 framing, topic, confidence, is_continuation, links)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (digest_id, item["cluster_id"], pos, item["headline"], item["summary"],
             item.get("context", ""), item.get("framing", ""), item.get("topic", ""),
             item.get("confidence", "high"), bool(item.get("is_continuation")),
             _json.dumps(item.get("_links", []), ensure_ascii=False)),
        )
    return digest_id


def mark_published(conn, digest_id: int, items: list[dict[str, Any]], message_id: int | None) -> None:
    conn.execute(
        """
        UPDATE digests SET status = 'published', published_at = now(),
               telegram_message_id = %s WHERE id = %s
        """,
        (message_id, digest_id),
    )
    # запоминаем состояние кластера на момент публикации — на этом стоит правило повтора
    for item in items:
        conn.execute(
            """
            UPDATE clusters SET last_published_digest_id = %s, last_published_at = now(),
                   n_articles_at_publish = %s
            WHERE id = %s
            """,
            (digest_id, item.get("n_articles"), item["cluster_id"]),
        )


def _send_all(chat_id: str, messages: list[str]) -> int | None:
    first_id = None
    for msg in messages:
        result = send_message(chat_id, msg)
        first_id = first_id or result.get("message_id")
    return first_id


def wait_for_decision(digest_id: int, timeout_hours: float) -> str:
    """Ждёт нажатия кнопки. Без ответа за timeout — не публикуем (§3.11)."""
    deadline = time.monotonic() + timeout_hours * 3600
    offset: int | None = None

    while time.monotonic() < deadline:
        try:
            updates = get_updates(offset=offset, timeout=30)
        except TelegramError as exc:
            log.warning("getUpdates: %s", exc)
            time.sleep(10)
            continue

        for upd in updates:
            offset = upd["update_id"] + 1
            cq = upd.get("callback_query")
            if not cq:
                continue
            data = cq.get("data", "")
            if not data.endswith(f":{digest_id}"):
                continue

            action = data.split(":")[0]
            answer_callback(cq["id"], "Публикую…" if action == "pub" else "Пропускаю")
            msg = cq.get("message", {})
            if msg:
                edit_reply_markup(str(msg["chat"]["id"]), msg["message_id"])
            return action

    return "timeout"


def run(items: list[dict[str, Any]], messages: list[str], gate_report: dict | None,
        dry_run: bool = True) -> dict[str, Any]:
    s = get_settings()
    review_mode = str(env("REVIEW_MODE", "")).lower() in ("1", "true", "yes") or bool(
        s.require("publish.review_mode")
    )
    stats: dict[str, Any] = {"review_mode": review_mode, "messages": len(messages)}

    if dry_run:
        log.info("DRY-RUN: пост не отправляется. Ниже то, что ушло бы:\n")
        for msg in messages:
            print("\n" + "─" * 70 + "\n" + msg)
        print("\n" + "─" * 70)
        stats["status"] = "dry-run"
        return stats

    from ..telegram import review_chat_id
    owner = review_chat_id()
    if not owner:
        raise RuntimeError("не задан REVIEW_CHAT_ID (или TELEGRAM_OWNER_CHAT_ID)")

    with connect() as conn:
        digest_id = save_digest(
            conn, items, messages,
            "pending_review" if review_mode else "draft", gate_report,
        )
    stats["digest_id"] = digest_id

    if not review_mode:
        channel = env("TELEGRAM_CHANNEL_ID", required=True)
        message_id = _send_all(channel, messages)
        with connect() as conn:
            mark_published(conn, digest_id, items, message_id)
        stats["status"] = "published"
        log.info("Опубликовано в канал, message_id=%s", message_id)
        return stats

    # режим ревью: показываем владельцу и ждём кнопку
    _send_all(owner, messages)
    send_message(
        owner,
        f"⬆️ Выпуск #{digest_id}: {len(items)} пунктов. Публикуем?",
        reply_markup=review_keyboard(digest_id),
    )

    timeout_hours = float(s.require("publish.review_timeout_hours"))
    decision = wait_for_decision(digest_id, timeout_hours)
    stats["decision"] = decision

    if decision == "pub":
        channel = env("TELEGRAM_CHANNEL_ID", required=True)
        message_id = _send_all(channel, messages)
        with connect() as conn:
            mark_published(conn, digest_id, items, message_id)
        stats["status"] = "published"
    else:
        with connect() as conn:
            conn.execute("UPDATE digests SET status = 'skipped' WHERE id = %s", (digest_id,))
        stats["status"] = "skipped"
        if decision == "timeout":
            notify_owner(f"Выпуск #{digest_id} не опубликован: за {timeout_hours} ч не было ответа.")

    log.info("Режим ревью, решение: %s", decision)
    return stats
