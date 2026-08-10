"""Стадии fetch → normalize → dedupe.

Собраны в один модуль: они всегда идут подряд и делят одно сетевое окно.
Битый источник логируется и не роняет прогон (§3.1).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import feedparser

from ..config import get_settings
from ..db import body_expiry, connect, insert_article, mark_fetch, title_exists
from ..net import Limiter, RobotsCache, fetch, make_client
from ..textutil import canonical_url, strip_html, title_hash

log = logging.getLogger(__name__)


@dataclass
class IngestStats:
    feeds_total: int = 0
    feeds_ok: int = 0
    feeds_failed: int = 0
    feeds_not_modified: int = 0
    entries_seen: int = 0
    entries_too_old: int = 0
    dup_url: int = 0
    dup_title: int = 0
    inserted: int = 0
    bodies_fetched: int = 0
    bodies_short: int = 0
    bodies_failed: int = 0
    bodies_blocked_by_robots: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def feed_success_ratio(self) -> float:
        return self.feeds_ok / self.feeds_total if self.feeds_total else 0.0

    def as_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "failures"}
        d["feed_success_ratio"] = round(self.feed_success_ratio, 3)
        d["failures"] = self.failures[:20]
        return d


def _entry_datetime(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        tm = entry.get(key)
        if tm:
            try:
                return datetime(*tm[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _entry_summary(entry) -> str:
    best = ""
    for block in entry.get("content", []) or []:
        val = strip_html(block.get("value", ""))
        if len(val) > len(best):
            best = val
    for key in ("summary", "description"):
        val = strip_html(entry.get(key, "") or "")
        if len(val) > len(best):
            best = val
    return best


def parse_feed(source: dict[str, Any], body: str, stats: IngestStats) -> list[dict[str, Any]]:
    """Записи фида -> нормализованные словари статей (§3.2)."""
    s = get_settings()
    max_age = timedelta(hours=int(s.require("fetch.max_article_age_hours")))
    now = datetime.now(timezone.utc)

    parsed = feedparser.parse(body)
    out: list[dict[str, Any]] = []
    seen_in_feed: set[str] = set()

    for entry in parsed.entries:
        stats.entries_seen += 1
        link = entry.get("link") or ""
        url = canonical_url(link)
        if not url:
            continue

        published = _entry_datetime(entry)
        if published and now - published > max_age:
            stats.entries_too_old += 1
            continue

        title = strip_html(entry.get("title", "")).strip()
        if not title:
            continue

        # дедуп внутри одной выдачи фида
        if url in seen_in_feed:
            stats.dup_url += 1
            continue
        seen_in_feed.add(url)

        out.append(
            {
                "source_id": source["id"],
                "url_canonical": url,
                # исходный адрес из фида — по нему ходим и его ставим в пост;
                # канонический годится как ключ, но не всегда открывается
                "url": link.strip(),
                "title": title,
                "summary_feed": _entry_summary(entry)[: s.require("normalize.max_body_chars")],
                "body": None,
                "body_expires_at": None,
                "title_hash": title_hash(title),
                "published_at": published or now,
            }
        )
    return out


def extract_body(html: str) -> str:
    """Основной текст страницы. trafilatura — стандартная библиотека извлечения."""
    try:
        import trafilatura
    except ImportError:  # pragma: no cover
        return ""
    try:
        text = trafilatura.extract(
            html,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        )
        return (text or "").strip()
    except Exception as exc:  # noqa: BLE001 — извлечение не должно ронять прогон
        log.debug("trafilatura упала: %s", exc)
        return ""


async def _fetch_body(
    client, robots: RobotsCache, limiter: Limiter, art: dict[str, Any], stats: IngestStats
) -> None:
    """Тянет полный текст. Пейволл/короткий текст — не ошибка, работаем с анонсом (§3.2)."""
    s = get_settings()
    url = art["url"] or art["url_canonical"]

    if not await robots.allowed(client, url):  # §5.6
        stats.bodies_blocked_by_robots += 1
        return

    async with limiter.slot(url):
        res = await fetch(client, url)
    if not res.ok:
        stats.bodies_failed += 1
        return

    text = extract_body(res.text)
    if len(text) < int(s.require("normalize.min_body_chars")):
        stats.bodies_short += 1
        return

    art["body"] = text[: int(s.require("normalize.max_body_chars"))]
    art["body_expires_at"] = body_expiry()  # §5.4
    stats.bodies_fetched += 1


async def _ingest_source(
    client, robots: RobotsCache, limiter: Limiter, source: dict[str, Any], stats: IngestStats
) -> list[dict[str, Any]]:
    async with limiter.slot(source["feed_url"]):
        res = await fetch(
            client, source["feed_url"],
            etag=source.get("etag"), last_modified=source.get("last_modified"),
        )

    if res.not_modified:
        stats.feeds_not_modified += 1
        stats.feeds_ok += 1
        source["_ok"] = True
        return []

    if not res.ok:
        stats.feeds_failed += 1
        source["_ok"] = False
        stats.failures.append(f"{source['id']}: {res.error or f'HTTP {res.status}'}")
        log.warning("Фид %s не отдался: %s", source["id"], res.error or res.status)
        return []

    stats.feeds_ok += 1
    source["_ok"] = True
    articles = parse_feed(source, res.text, stats)
    source["_etag"] = res.etag
    source["_last_modified"] = res.last_modified

    if get_settings().get_path("normalize.body_fetch_enabled", True):
        await asyncio.gather(
            *[_fetch_body(client, robots, limiter, a, stats) for a in articles]
        )

    return articles


async def run_async(dry_run: bool = True, limit_sources: int | None = None) -> tuple[IngestStats, list[dict]]:
    from ..db import active_sources, sync_sources

    stats = IngestStats()
    robots = RobotsCache()
    limiter = Limiter()  # общий на весь прогон, иначе потолок параллелизма не работает

    with connect() as conn:
        sync_sources(conn)
        sources = active_sources(conn)
    if limit_sources:
        sources = sources[:limit_sources]
    stats.feeds_total = len(sources)

    async with make_client() as client:
        results = await asyncio.gather(
            *[_ingest_source(client, robots, limiter, src, stats) for src in sources]
        )

    all_articles = [a for batch in results for a in batch]

    if dry_run:
        return stats, all_articles

    with connect() as conn:
        for src in sources:
            mark_fetch(
                conn, src["id"], bool(src.get("_ok")),
                src.get("_etag"), src.get("_last_modified"),
            )

        for art in all_articles:
            # второй контур дедупа: тот же заголовок в том же источнике (§3.3)
            if title_exists(conn, art["source_id"], art["title_hash"]):
                stats.dup_title += 1
                continue
            if insert_article(conn, art) is None:
                stats.dup_url += 1
            else:
                stats.inserted += 1

    return stats, all_articles


def run(dry_run: bool = True, limit_sources: int | None = None):
    return asyncio.run(run_async(dry_run=dry_run, limit_sources=limit_sources))
