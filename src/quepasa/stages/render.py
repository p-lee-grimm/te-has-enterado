"""Стадия render — HTML-разметка Telegram (§3.9).

Ссылки обязательны у каждого пункта и обязаны вести на издания РАЗНЫХ полюсов:
показать, как сюжет подают разные стороны, — половина смысла продукта.
Длинный пост режется по границе пункта, а не посреди текста.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..config import get_settings

log = logging.getLogger(__name__)

MONTHS_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

SEPARATOR = "——————"


def local_now() -> datetime:
    tz = ZoneInfo(get_settings().require("render.timezone"))
    return datetime.now(tz)


def format_date(dt: datetime | None = None) -> str:
    dt = dt or local_now()
    return f"{dt.day} {MONTHS_RU[dt.month - 1]}"


def pick_links(articles: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """До limit ссылок на разные издания, максимально разнесённые по полюсам.

    Требование §3.9: не три левых подряд. Берём по одному материалу с издания,
    затем обходим полюса от краёв спектра к центру.
    """
    by_source: dict[str, dict[str, Any]] = {}
    for art in articles:
        if not art.get("url") and not art.get("url_canonical"):
            continue
        if art["source_id"] not in by_source:
            by_source[art["source_id"]] = art

    pool = list(by_source.values())
    if len(pool) <= limit:
        return pool

    buckets: dict[str, list[dict[str, Any]]] = {}
    for art in pool:
        buckets.setdefault(art["lean"], []).append(art)

    # чередуем края спектра: left, right, center-left, center-right, center
    order = ["left", "right", "center-left", "center-right", "center"]
    picked: list[dict[str, Any]] = []
    while len(picked) < limit and any(buckets.get(l) for l in order):
        for lean in order:
            if len(picked) >= limit:
                break
            if buckets.get(lean):
                picked.append(buckets[lean].pop(0))
    return picked


def render_item(index: int, item: dict[str, Any]) -> str:
    s = get_settings()
    links_n = int(s.require("render.links_per_item"))
    esc = html.escape

    headline = esc(item["headline"].strip())
    if item.get("is_continuation"):
        headline = f"{headline} <i>(продолжение)</i>"

    lines = [f"{index}. <b>{headline}</b>", esc(item["summary"].strip())]

    if item.get("context", "").strip():
        lines.append(f"\n<i>Контекст:</i> {esc(item['context'].strip())}")
    if item.get("framing", "").strip():
        lines.append(f"<i>Как подают:</i> {esc(item['framing'].strip())}")
    if item.get("confidence") == "low":
        lines.append("<i>источники расходятся</i>")

    links = pick_links(item.get("all_articles") or item.get("articles", []), links_n)
    if links:
        rendered = " · ".join(
            f'<a href="{esc(a.get("url") or a["url_canonical"], quote=True)}">'
            f'{esc(a["source_name"])}</a>'
            for a in links
        )
        lines.append(f"\n{rendered}")

    item["_links"] = [
        {
            "url": a.get("url") or a["url_canonical"],
            "source_id": a["source_id"],
            "source_name": a["source_name"],
            "lean": a["lean"],
        }
        for a in links
    ]
    return "\n".join(lines)


def render(items: list[dict[str, Any]], dt: datetime | None = None) -> list[str]:
    """Возвращает список сообщений: одно, если влезает, иначе несколько."""
    limit = int(get_settings().require("render.telegram_message_limit"))
    header = f"📅 Испания, {format_date(dt)}"

    blocks = [render_item(i, item) for i, item in enumerate(items, 1)]

    messages: list[str] = []
    current = header
    for n, block in enumerate(blocks):
        # разделитель между пунктами, но не перед первым и не после переноса
        sep = f"\n\n{SEPARATOR}" if n and current and current != header else ""
        candidate = f"{current}{sep}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        # не влезает — закрываем сообщение и начинаем новое с этого же пункта
        if current and current != header:
            messages.append(current)
        current = block
        if len(current) > limit:
            # один пункт длиннее лимита: режем по строкам, не рвём слова
            messages.extend(_split_hard(current, limit))
            current = ""
    if current:
        messages.append(current)

    for i in range(1, len(messages)):
        messages[i] = f"<i>(продолжение {i + 1}/{len(messages)})</i>\n\n{messages[i]}"
    return messages


def _split_hard(text: str, limit: int) -> list[str]:
    out, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > limit:
            if buf:
                out.append(buf)
            buf = line[:limit]
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        out.append(buf)
    return out


def run(items: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    messages = render(items)
    stats = {
        "items": len(items),
        "messages": len(messages),
        "chars": sum(len(m) for m in messages),
        "items_without_links": sum(1 for i in items if not i.get("_links")),
    }
    log.info(
        "Свёрстано пунктов: %s, сообщений: %s, символов: %s",
        stats["items"], stats["messages"], stats["chars"],
    )
    return stats, messages
