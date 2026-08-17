"""Смысловые правки опубликованных постов (§2).

Ранние числа и формулировки меняются в течение часов: количество пострадавших,
суммы, статус задержанных. Механическая правка (добавить ссылку) идёт
автоматически, смысловая — только через ревью: автоматическая перезапись
опубликованного текста недопустима, цена ошибки выше цены задержки.

Существенная правка сопровождается строкой «Обновлено» — исправление молча
меняет то, что читатели уже переслали в свои чаты.
"""

from __future__ import annotations

import html
import logging
from typing import Any

from .config import get_settings, load_prompt
from .db import connect
from .llm import LLMUsage

log = logging.getLogger(__name__)

UPDATED_PREFIX = "Обновлено:"


def _diff_call(published_md: str, titles: list[str], usage: LLMUsage) -> dict[str, Any]:
    from .llm import json_call as _llm_json

    user = (
        f"НАШ ОПУБЛИКОВАННЫЙ ПОСТ:\n{published_md}\n\n"
        "ЗАГОЛОВКИ ИСТОЧНИКОВ СЕЙЧАС:\n"
        + "\n".join(f"- {t}" for t in titles[:15])
    )
    return _llm_json(load_prompt("fact_diff.md"), user, usage, retries=1)


def candidates(conn) -> list[dict[str, Any]]:
    """Посты в окне смысловой правки."""
    hours = int(get_settings().get_path("autopost.edit_window_hours", 12))
    return conn.execute(
        """
        SELECT p.*, c.n_articles
        FROM posts p JOIN clusters c ON c.id = p.cluster_id
        WHERE p.status = 'published' AND p.message_id IS NOT NULL
          AND p.published_at >= now() - make_interval(hours => %s)
          -- если правка уже ждёт решения, второй раз не спрашиваем
          AND NOT EXISTS (
              SELECT 1 FROM post_edits e
              WHERE e.post_id = p.id AND e.status = 'pending'
          )
        """,
        (hours,),
    ).fetchall()


def check_post(conn, post: dict[str, Any]) -> dict[str, Any] | None:
    """Сверяет пост с текущими заголовками. Возвращает черновик правки или None."""
    rows = conn.execute(
        """
        SELECT a.title FROM articles a
        WHERE a.cluster_id = %s ORDER BY a.published_at DESC LIMIT 15
        """,
        (post["cluster_id"],),
    ).fetchall()
    titles = [r["title"] for r in rows]
    if not titles:
        return None

    usage = LLMUsage()
    try:
        data = _diff_call(post["header_md"], titles, usage)
    except Exception as exc:  # noqa: BLE001 — один пост не роняет прогон
        log.warning("Сверка фактов по посту %s не удалась: %s", post["id"], exc)
        return None

    if not data.get("changed"):
        return None

    headline = (data.get("headline") or "").strip()
    lead = (data.get("lead") or "").strip()
    if not headline:
        return None

    # Те же правила имён, что и при сборке поста. Правка идёт отдельным
    # вызовом модели и мимо generate_header, поэтому без этого «ПП» и
    # транскрипции возвращаются в уже вычищенный пост.
    from .posts import fix_names, restore_latin_names

    headline = restore_latin_names(fix_names(headline), titles)
    lead = restore_latin_names(fix_names(lead), titles)
    what = restore_latin_names(fix_names((data.get("what") or "").strip()), titles)

    new_header = f"**{headline}**" + (f"\n\n{lead}" if lead else "")
    return {
        "post_id": post["id"],
        "new_header": new_header,
        "what_changed": what,
        "cost_usd": round(usage.cost_usd, 4),
    }


def send_for_review(conn, post: dict[str, Any], draft: dict[str, Any]) -> None:
    edit_id = conn.execute(
        """
        INSERT INTO post_edits (post_id, new_header, what_changed)
        VALUES (%s, %s, %s) RETURNING id
        """,
        (draft["post_id"], draft["new_header"], draft["what_changed"]),
    ).fetchone()["id"]

    _send(edit_id, post, draft)


def _send(edit_id: int, post: dict[str, Any], draft: dict[str, Any]) -> None:
    """Сообщение о расхождении с кнопками. Отдельно от вставки в базу:
    ту же правку надо уметь показать заново, не заводя вторую запись."""
    from .telegram import message_link, notify_owner

    link = message_link(post["message_id"])
    esc = html.escape
    text = "\n".join([
        "<b>Факты в опубликованном посте разошлись с источниками</b>",
        "",
        f"<b>Что изменилось:</b> {esc(draft['what_changed'])}",
        "",
        "<b>Было:</b>",
        esc(post["header_md"])[:600],
        "",
        "<b>Стало:</b>",
        esc(draft["new_header"])[:600],
        "",
        f'<a href="{link}">Пост в канале</a>' if link else "",
        "<i>Замена ручная: автоматически переписывать опубликованное нельзя.</i>",
    ])
    notify_owner(text, reply_markup={
        "inline_keyboard": [[
            {"text": "✅ Заменить", "callback_data": f"edit:apply:{edit_id}"},
            {"text": "✖️ Оставить", "callback_data": f"edit:skip:{edit_id}"},
        ]]
    })


def resend_pending(conn, limit: int = 10) -> int:
    """Показывает заново правки, ждущие решения, с их прежними id."""
    rows = conn.execute(
        """
        SELECT e.id, e.new_header, e.what_changed, p.header_md, p.message_id
        FROM post_edits e JOIN posts p ON p.id = e.post_id
        WHERE e.status = 'pending' ORDER BY e.id LIMIT %s
        """,
        (limit,),
    ).fetchall()
    for r in rows:
        _send(r["id"], {"header_md": r["header_md"], "message_id": r["message_id"]},
              {"new_header": r["new_header"], "what_changed": r["what_changed"] or ""})
    return len(rows)


def apply_edit(conn, edit_id: int) -> bool:
    """Применяет одобренную правку и дописывает строку «Обновлено»."""
    from .config import env
    from .entities import render_cards_html
    from .posts import cluster_articles, compose_html
    from .telegram import edit_message_text

    row = conn.execute(
        """
        SELECT e.*, p.cluster_id, p.message_id, p.category, p.significance,
               p.one_sided, p.entity_ids, p.entity_context, p.geo_tag, p.related_md
        FROM post_edits e JOIN posts p ON p.id = e.post_id
        WHERE e.id = %s AND e.status = 'pending'
        """,
        (edit_id,),
    ).fetchone()
    if row is None:
        return False

    articles = cluster_articles(conn, row["cluster_id"])
    cards = [dict(r) for r in conn.execute(
        "SELECT * FROM entities WHERE id = ANY(%s)",
        (list(row["entity_ids"] or []),),
    ).fetchall()]

    # строка «Обновлено» обязательна: исправление молча меняет то, что уже
    # переслали в чаты, и читатель должен видеть, что текст поменялся
    header = row["new_header"]
    if row["what_changed"]:
        header = f"{header}\n\n_{UPDATED_PREFIX} {row['what_changed']}_"

    text = compose_html(
        header, articles, row["category"],
        one_sided=bool(row["one_sided"]),
        significance=row["significance"] or "",
        cards_html=render_cards_html(cards, row["entity_context"]), cards=cards,
        related_md=row["related_md"] or "", geo_tag=row["geo_tag"],
    )
    edit_message_text(env("TELEGRAM_CHANNEL_ID"), int(row["message_id"]), text)

    conn.execute(
        "UPDATE posts SET header_md = %s, edited_at = now(), "
        "edit_count = edit_count + 1 WHERE id = %s",
        (header, row["post_id"]),
    )
    conn.execute(
        "UPDATE post_edits SET status = 'applied', decided_at = now() WHERE id = %s",
        (edit_id,),
    )
    log.info("Пост %s исправлен: %s", row["post_id"], row["what_changed"])
    return True


def run(dry_run: bool = True) -> dict[str, Any]:
    """Проверяет посты в окне и отправляет расхождения на ревью."""
    stats = {"checked": 0, "changed": 0, "cost_usd": 0.0}
    with connect() as conn:
        posts_ = candidates(conn)
    stats["checked"] = len(posts_)

    for post in posts_:
        with connect() as conn:
            draft = check_post(conn, post)
            if draft is None:
                continue
            stats["changed"] += 1
            stats["cost_usd"] += draft["cost_usd"]
            if dry_run:
                log.info("DRY-RUN: пост %s — %s", post["id"], draft["what_changed"])
                continue
            send_for_review(conn, post, draft)

    log.info("Сверка фактов: проверено %s, расхождений %s",
             stats["checked"], stats["changed"])
    return stats
