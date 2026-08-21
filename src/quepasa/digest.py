"""Дайджест: всё, что не дотянуло до отдельного поста, одним постом.

Второй ярус публикации. Отдельный пост получает сюжет, который заметили с
разных сторон спектра или о котором пишут многие; остальное, что всё же
является общей повесткой (несколько источников), собирается сюда.

Категории мелкие по своей природе — за сутки набирается несколько сюжетов на
всё. Поэтому категория, в которой меньше min_items_per_category пунктов,
сливается в блок «Прочее», а не занимает отдельный заголовок ради одной строки.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .config import get_settings
from .db import connect
from .markup import markdown_to_telegram_html
from .posts import cluster_articles
from .telegram import send_message

log = logging.getLogger(__name__)

OTHER = "Прочее"

# Порядок блоков в посте: сначала то, что влияет на жизнь, потом остальное
CATEGORY_ORDER = [
    "политика", "экономика", "общество", "регионы",
    "международное", "происшествия", "культура", "спорт",
]


def _source_marks(articles: list[dict[str, Any]]) -> str:
    """Ссылки на издания со значком позиции, упорядоченные по шкале слева направо.

    Число ссылок ограничено жёстко: строка дайджеста со всеми источниками
    крупного сюжета — это тысяча символов, и пост целиком не влезает
    в лимит Telegram. Берём края спектра: они показывают охват, а середина
    добавляет длину, но не смысл.
    """
    from .spectrum import lean_value

    limit = int(get_settings().get_path("digest.links_per_item", 2))

    usable = [a for a in articles if a.get("url") or a.get("url_canonical")]
    if not usable:
        return ""

    # по одному материалу с издания, отсортированные по позиции на шкале
    by_source: dict[str, dict[str, Any]] = {}
    for a in usable:
        by_source.setdefault(a["source_id"], a)
    ordered = sorted(
        by_source.values(),
        key=lambda a: (lean_value(a["lean"]) if a.get("type") != "official" else 99) or 0,
    )

    if len(ordered) > limit:
        # берём крайних слева и справа: они и показывают размах
        picked = [ordered[0], ordered[-1]]
        # если лимит больше двух — добираем из середины
        middle = ordered[1:-1]
        while len(picked) < limit and middle:
            picked.insert(len(picked) - 1, middle.pop(len(middle) // 2))
        ordered = picked

    # Значок на группу, а не на издание: «➡️ ABC · ➡️ OKdiario» — это один
    # и тот же фланг, названный дважды.
    from .posts import group_sources_md

    return group_sources_md(ordered)


def group_by_category(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Раскладывает по категориям, мелкие сливает в «Прочее»."""
    min_per = int(get_settings().get_path("digest.min_items_per_category", 3))

    buckets: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        buckets.setdefault(it.get("topic") or OTHER, []).append(it)

    grouped: dict[str, list[dict[str, Any]]] = {}
    other: list[dict[str, Any]] = []
    for cat, group in buckets.items():
        if cat == OTHER or len(group) < min_per:
            other.extend(group)
        else:
            grouped[cat] = group

    ordered = {c: grouped[c] for c in CATEGORY_ORDER if c in grouped}
    for cat, group in grouped.items():  # категории вне списка — следом
        ordered.setdefault(cat, group)
    if other:
        ordered[OTHER] = other
    return ordered


def shorten(text: str, limit: int) -> str:
    """Укорачивает по границе слова: обрыв посреди слова читается как ошибка."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:—-")
    return f"{cut}…"


def top_posts(conn, limit: int) -> list[dict[str, Any]]:
    """Лучшие посты канала за сутки — для блока «Главное за сегодня».

    Ссылки ведут внутрь канала, а не на издания: блок нужен для навигации,
    иначе он дублирует «Коротко» теми же внешними ссылками.
    """
    return conn.execute(
        """
        SELECT p.cluster_id, p.header_md, p.message_id, c.score
        FROM posts p JOIN clusters c ON c.id = p.cluster_id
        WHERE p.status = 'published'
          AND p.message_id IS NOT NULL
          AND p.published_at >= now() - interval '24 hours'
        ORDER BY c.score DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()


def render_main_block(rows: list[dict[str, Any]], channel: str | None = None) -> str:
    """«Главное за сегодня» — ссылки на собственные посты канала."""
    from .telegram import message_link

    if not rows:
        return ""
    width = int(get_settings().get_path("digest.main_headline_chars", 60))

    lines = []
    for r in rows:
        head = shorten((r["header_md"] or "").strip().split("\n")[0].strip("* "), width)
        url = message_link(r["message_id"], channel)
        lines.append(f"• [{head}]({url})" if url else f"• {head}")
    return "**Главное за сегодня**\n" + "\n".join(lines)


def split_messages(items: list[dict[str, Any]], date_label: str,
                   main_block: str = "") -> list[str]:
    """Режет дайджест на сообщения по границе пункта.

    Отбрасывать лишнее молча нельзя — пропадает то, чего никто не хватится.
    Поэтому сначала пытаемся уместить всё в цепочку сообщений (второе и далее
    уходят реплаем на первое), и только если цепочка вырастает сверх
    max_messages, лишние пункты отбрасываются: четыре сообщения подряд
    вечером — это стена, а не дайджест.
    """
    s = get_settings()
    limit = int(s.get_path("render.telegram_message_limit", 4096))
    max_msgs = int(s.get_path("digest.max_messages", 3))

    def html_len(chunk, block):
        return len(markdown_to_telegram_html(render_md(chunk, date_label, block)))

    messages: list[str] = []
    rest = list(items)
    block = main_block

    while rest and len(messages) < max_msgs:
        chunk = rest
        # уменьшаем кусок, пока он не влезет
        while chunk and html_len(chunk, block) > limit:
            chunk = chunk[:-1]
        if not chunk:
            break
        messages.append(render_md(chunk, date_label, block))
        rest = rest[len(chunk):]
        # «Главное» и дата — только в первом сообщении
        block, date_label = "", date_label + " (продолжение)"

    return messages


def render_md(items: list[dict[str, Any]], date_label: str,
              main_block: str = "") -> str:
    grouped = group_by_category(items)
    blocks = []
    if main_block:
        blocks.append(main_block)
    blocks.append(f"**Коротко — {date_label}**")

    for cat, group in grouped.items():
        # Заголовок категории и её пункты — один блок: пустая строка после
        # заголовка и между пунктами растягивает дайджест на два экрана,
        # а группировку и так видно по заголовкам. Пустая строка остаётся
        # только между категориями.
        lines = [f"**{cat.capitalize()}**"]
        for it in group:
            marks = _source_marks(it["articles"])
            lines.append(f"• {it['headline']}\n{marks}" if marks else f"• {it['headline']}")
        blocks.append("\n".join(lines))

    blocks.append("#дайджест")
    return "\n\n".join(blocks)


def build(dry_run: bool = True) -> dict[str, Any]:
    """Собирает дайджест: отбор -> заголовки моделью -> вёрстка."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from .posts import digest_clusters, generate_header
    from .stages.render import MONTHS_RU

    s = get_settings()
    tz = ZoneInfo(s.require("render.timezone"))
    now = datetime.now(tz)
    date_label = f"{now.day} {MONTHS_RU[now.month - 1]}"

    stats: dict[str, Any] = {"candidates": 0, "items": 0, "errors": 0}

    if not s.get_path("digest.enabled", True):
        stats["skipped"] = "digest.enabled = false"
        return stats

    with connect() as conn:
        candidates = digest_clusters(conn)
        stats["candidates"] = len(candidates)
        if not candidates:
            log.info("Дайджест: нечего публиковать")
            return stats
        articles = {c["cluster_id"]: cluster_articles(conn, c["cluster_id"])
                    for c in candidates}

    items: list[dict[str, Any]] = []
    for c in candidates:
        cid = c["cluster_id"]
        try:
            header, topic, _ = generate_header(cid)
        except Exception as exc:  # noqa: BLE001 — один сюжет не роняет дайджест
            log.warning("Дайджест, сюжет %s: %s", cid, exc)
            stats["errors"] += 1
            continue
        items.append({
            "cluster_id": cid,
            "headline": header.split("\n")[0].strip("* "),
            "topic": topic,
            "articles": articles[cid],
        })

    stats["items"] = len(items)
    min_lines = int(s.get_path("digest.min_lines", 3))
    if len(items) < min_lines:
        # три строки — не дайджест, а обрывок: лучше промолчать
        stats["skipped"] = f"строк {len(items)} при минимуме {min_lines}"
        log.info("Вечерний пост не публикуется: %s", stats["skipped"])
        return stats


    with connect() as conn:
        main_rows = top_posts(conn, int(s.get_path("digest.main_items", 3)))
    main_block = render_main_block(main_rows)
    stats["main_items"] = len(main_rows)

    parts = split_messages(items, date_label, main_block)
    stats["messages"] = len(parts)

    if dry_run:
        log.info("DRY-RUN, дайджест не отправлен (%s сообщ.):\n%s",
                 len(parts), "\n\n--- следующее сообщение ---\n\n".join(parts))
        return stats

    from .config import env

    # Вечерний пост — единственное регулярное уведомление канала, поэтому
    # со звуком и СВЕРХ квоты autopost.sound: это решение владельца.
    loud = bool(s.get_path("digest.with_sound", True))
    channel = env("TELEGRAM_CHANNEL_ID", required=True)

    message_id = None
    for n, part in enumerate(parts):
        html_part = markdown_to_telegram_html(part)
        # продолжения — реплаем на первое сообщение и всегда беззвучно:
        # это один логический пост, а не несколько новостей
        res = send_message(
            channel, html_part,
            silent=not loud or n > 0,
            reply_to_message_id=message_id if n else None,
        )
        if n == 0:
            message_id = res.get("message_id")
    html = markdown_to_telegram_html(parts[0])

    with connect() as conn:
        digest_id = conn.execute(
            """
            INSERT INTO digests (status, published_at, telegram_message_id,
                                 item_count, body_html)
            VALUES ('published', now(), %s, %s, %s) RETURNING id
            """,
            (message_id, len(items), html),
        ).fetchone()["id"]

        for pos, it in enumerate(items, 1):
            conn.execute(
                """
                INSERT INTO digest_items
                    (digest_id, cluster_id, position, headline, summary,
                     topic, links)
                VALUES (%s,%s,%s,%s,'',%s,%s)
                """,
                (digest_id, it["cluster_id"], pos, it["headline"], it["topic"],
                 json.dumps([
                     {"url": a.get("url") or a["url_canonical"],
                      "source_id": a["source_id"], "source_name": a["source_name"],
                      "lean": a["lean"]}
                     for a in it["articles"]
                 ], ensure_ascii=False)),
            )
            # Сюжет попал в дайджест — отдельным постом он больше не выйдет.
            #
            # Без ON CONFLICT: уникального индекса по cluster_id нет и быть
            # не должно — повторный пост о развившемся сюжете разрешён
            # (autopost.repeat_min_hours). ON CONFLICT ссылался на
            # ограничение из миграции, которого в базе нет, и весь вечерний
            # прогон падал — уже ПОСЛЕ отправки сообщений в канал.
            conn.execute(
                """
                INSERT INTO posts (cluster_id, header_md, category, status)
                SELECT %s, %s, %s, 'skipped'
                WHERE NOT EXISTS (SELECT 1 FROM posts WHERE cluster_id = %s)
                """,
                (it["cluster_id"], it["headline"], it["topic"], it["cluster_id"]),
            )

    stats["digest_id"] = digest_id
    stats["message_id"] = message_id
    log.info("Дайджест опубликован: %s пунктов, message_id=%s", len(items), message_id)
    return stats
