"""Стадия select — информационный минимум (§3.7).

Жёсткое правило: кластер идёт в выпуск только при ≥3 уникальных источниках.
Один источник — это не общая повестка, а частный материал издания.

Правило повтора: уже опубликованный сюжет возвращается только при значимом
обновлении, и тогда помечается как продолжение — промпт получит наш прошлый
пересказ и напишет «что нового», а не перескажет всё заново.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import get_settings
from ..db import connect

log = logging.getLogger(__name__)


def _articles_for_cluster(conn, cluster_id: int) -> list[dict[str, Any]]:
    """Статьи кластера с приоритетом на разные источники и разные полюса.

    DISTINCT ON по источнику даёт по одному лучшему материалу с издания —
    так на вход пересказу попадают разные точки зрения, а не десять текстов
    одной редакции.
    """
    return conn.execute(
        """
        SELECT DISTINCT ON (a.source_id)
               a.id, a.title, a.url, a.url_canonical, a.summary_feed, a.body,
               a.published_at, a.source_id, s.name AS source_name, s.lean, s.type
        FROM articles a
        JOIN sources s ON s.id = a.source_id
        WHERE a.cluster_id = %s
        ORDER BY a.source_id,
                 (a.body IS NOT NULL) DESC,
                 a.published_at DESC
        """,
        (cluster_id,),
    ).fetchall()


def _pick_diverse(articles: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Берём до limit статей, максимально разводя полюса (§3.8)."""
    by_lean: dict[str, list[dict]] = {}
    for art in articles:
        by_lean.setdefault(art["lean"], []).append(art)

    picked: list[dict[str, Any]] = []
    # раунд-робин по полюсам: сначала по одной с каждого, потом по второй и т.д.
    while len(picked) < limit and any(by_lean.values()):
        for lean in sorted(by_lean):
            if len(picked) >= limit:
                break
            if by_lean[lean]:
                picked.append(by_lean[lean].pop(0))
    return picked


def is_repeat_eligible(cluster: dict[str, Any]) -> tuple[bool, bool]:
    """(включать ли, продолжение ли) по правилу повтора §3.7."""
    s = get_settings()
    if cluster.get("last_published_at") is None:
        return True, False

    from datetime import datetime, timezone

    min_new = int(s.require("select.repeat_min_new_articles"))
    min_hours = int(s.require("select.repeat_min_hours"))

    at_publish = cluster.get("n_articles_at_publish") or 0
    new_articles = int(cluster["n_articles"]) - at_publish
    hours_since = (
        datetime.now(timezone.utc) - cluster["last_published_at"]
    ).total_seconds() / 3600

    # либо накопилось достаточно нового, либо прошло много времени и поток не иссяк
    if new_articles >= min_new:
        return True, True
    if hours_since >= min_hours and new_articles > 0:
        return True, True
    return False, True


def run(ranked: list[dict[str, Any]], dry_run: bool = True) -> tuple[dict[str, Any], list[dict]]:
    s = get_settings()
    max_items = int(s.require("select.max_items"))
    min_items = int(s.require("select.min_items"))
    min_sources = int(s.require("select.min_unique_sources"))
    max_articles = int(s.require("summarize.max_articles_per_cluster"))

    stats = {
        "candidates": len(ranked),
        "rejected_few_sources": 0,
        "rejected_repeat": 0,
        "selected": 0,
        "continuations": 0,
    }

    selected: list[dict[str, Any]] = []
    with connect() as conn:
        for cluster in ranked:
            if len(selected) >= max_items:
                break
            if cluster["n_sources"] < min_sources:
                stats["rejected_few_sources"] += 1
                continue

            include, is_continuation = is_repeat_eligible(cluster)
            if not include:
                stats["rejected_repeat"] += 1
                continue

            articles = _articles_for_cluster(conn, cluster["id"])
            if len({a["source_id"] for a in articles}) < min_sources:
                stats["rejected_few_sources"] += 1
                continue

            prev_summary = None
            if is_continuation:
                row = conn.execute(
                    """
                    SELECT summary FROM digest_items
                    WHERE cluster_id = %s ORDER BY id DESC LIMIT 1
                    """,
                    (cluster["id"],),
                ).fetchone()
                prev_summary = row["summary"] if row else None

            selected.append(
                {
                    "cluster_id": cluster["id"],
                    "score": cluster["score"],
                    "n_sources": cluster["n_sources"],
                    "n_articles": cluster["n_articles"],
                    "lean_spread": cluster["lean_spread"],
                    "is_continuation": is_continuation,
                    "previous_summary": prev_summary,
                    "articles": _pick_diverse(articles, max_articles),
                    "all_articles": articles,
                }
            )
            if is_continuation:
                stats["continuations"] += 1

    stats["selected"] = len(selected)

    if len(selected) < min_items:
        log.warning(
            "Отобрано %s пунктов при минимуме %s — выпуск не состоится",
            len(selected), min_items,
        )
    log.info(
        "Отобрано %s кластеров (продолжений %s, отсеяно по источникам %s, по повтору %s)",
        stats["selected"], stats["continuations"],
        stats["rejected_few_sources"], stats["rejected_repeat"],
    )
    return stats, selected


def enforce_topic_diversity(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Не больше N пунктов одной темы подряд (§3.7).

    Вызывается после summarize: тема известна только оттуда.
    """
    max_row = int(get_settings().require("select.max_same_topic_in_row"))
    out: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []

    for item in items:
        topic = item.get("topic", "")
        tail = [i.get("topic", "") for i in out[-max_row:]]
        if len(tail) == max_row and all(t == topic for t in tail):
            deferred.append(item)
        else:
            out.append(item)

    # отложенные возвращаем в конец, порядок между ними сохраняем
    for item in deferred:
        out.append(item)
    return out
