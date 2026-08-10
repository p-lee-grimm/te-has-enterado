"""Стадия rank — скор кластера (§3.6).

Линейная комбинация сигналов, умноженная на затухание по свежести.
Все веса в config/settings.yaml, в коде чисел нет.

Ключевая тонкость: n_sources считается по УНИКАЛЬНЫМ изданиям. Иначе одно
издание с десятью обновлениями перебивает настоящую общую новость.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from ..config import get_settings
from ..db import connect

log = logging.getLogger(__name__)


def _fetch_open_clusters(conn, window_hours: int, velocity_hours: int) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT
            c.id,
            c.first_seen_at,
            c.last_seen_at,
            c.last_published_at,
            c.last_published_digest_id,
            c.n_articles_at_publish,
            count(a.id)                                          AS n_articles,
            count(DISTINCT a.source_id)                          AS n_sources,
            count(DISTINCT s.lean)                               AS lean_spread,
            count(*) FILTER (
                WHERE a.published_at >= now() - make_interval(hours => %s)
            )                                                    AS velocity,
            bool_or(s.type = 'agency')                           AS agency_flag,
            bool_or(s.type = 'official')                         AS official_flag,
            max(a.published_at)                                  AS newest_at,
            avg(s.weight)                                        AS avg_weight
        FROM clusters c
        JOIN articles a ON a.cluster_id = c.id
        JOIN sources  s ON s.id = a.source_id
        WHERE c.status = 'open'
          AND c.last_seen_at >= now() - make_interval(hours => %s)
        GROUP BY c.id
        """,
        (velocity_hours, window_hours),
    ).fetchall()


def recency_factor(age_hours: float, half_life_hours: float) -> float:
    """Экспоненциальное затухание с заданным периодом полураспада."""
    if half_life_hours <= 0:
        return 1.0
    return math.exp(-math.log(2) * max(0.0, age_hours) / half_life_hours)


def score_cluster(row: dict[str, Any], now) -> float:
    s = get_settings()
    w = s.require("rank.weights")
    half_life = float(s.require("rank.recency_half_life_hours"))

    base = (
        float(w["n_sources"]) * float(row["n_sources"])
        + float(w["lean_spread"]) * float(row["lean_spread"])
        + float(w["n_articles"]) * float(row["n_articles"])
        + float(w["velocity"]) * float(row["velocity"])
        + float(w["agency_flag"]) * (1.0 if row["agency_flag"] else 0.0)
    )
    age_hours = (now - row["newest_at"]).total_seconds() / 3600 if row["newest_at"] else 0.0
    return base * recency_factor(age_hours, half_life) * float(row["avg_weight"] or 1.0)


def run(dry_run: bool = True) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from datetime import datetime, timezone

    s = get_settings()
    window = int(s.require("cluster.window_hours"))
    velocity_hours = int(s.require("rank.velocity_window_hours"))
    now = datetime.now(timezone.utc)

    with connect() as conn:
        rows = _fetch_open_clusters(conn, window, velocity_hours)
        for row in rows:
            row["score"] = score_cluster(row, now)
        rows.sort(key=lambda r: r["score"], reverse=True)

        if not dry_run:
            for row in rows:
                conn.execute(
                    "UPDATE clusters SET score = %s, n_articles = %s, n_sources = %s WHERE id = %s",
                    (row["score"], row["n_articles"], row["n_sources"], row["id"]),
                )

    stats = {
        "scored": len(rows),
        "with_3plus_sources": sum(1 for r in rows if r["n_sources"] >= 3),
        "top_score": round(rows[0]["score"], 2) if rows else 0,
    }
    log.info(
        "Ранжировано кластеров: %s, из них с ≥3 источниками: %s",
        stats["scored"], stats["with_3plus_sources"],
    )
    return stats, rows
