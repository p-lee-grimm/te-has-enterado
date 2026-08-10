"""Стадия cluster — инкрементальная онлайн-кластеризация (§3.5).

Сюжеты приходят непрерывно и живут во времени, поэтому батчевые алгоритмы
(KMeans/HDBSCAN) тут не годятся: они переразбивают всё заново на каждом прогоне
и ломают тождество сюжета между выпусками.

Алгоритм на статью:
  1. считаем близость к центроидам открытых кластеров (окно window_hours);
  2. максимум >= sim_threshold -> кладём туда и пересчитываем центроид;
  3. иначе заводим новый кластер;
  4. кластер закрывается, если в него ничего не падало idle_hours.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import get_settings
from ..db import connect

log = logging.getLogger(__name__)


def close_idle_clusters(conn) -> int:
    idle = int(get_settings().require("cluster.idle_hours"))
    row = conn.execute(
        """
        WITH closed AS (
            UPDATE clusters SET status = 'closed'
            WHERE status = 'open'
              AND last_seen_at < now() - make_interval(hours => %s)
            RETURNING 1
        )
        SELECT count(*) AS n FROM closed
        """,
        (idle,),
    ).fetchone()
    return row["n"]


def _unclustered(conn, limit: int) -> list[dict[str, Any]]:
    """Статьи без кластера, по возрастанию времени.

    Порядок важен: онлайн-алгоритм зависит от последовательности, и статья
    должна встречать те кластеры, которые к её моменту уже существовали.
    """
    return conn.execute(
        """
        SELECT id, embedding, published_at, source_id
        FROM articles
        WHERE cluster_id IS NULL AND embedding IS NOT NULL
        ORDER BY published_at ASC NULLS LAST, id ASC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()


def _best_open_cluster(conn, embedding, published_at) -> tuple[int | None, float]:
    """Ближайший открытый кластер в окне. Расстояние pgvector <=> это 1 - cosine."""
    s = get_settings()
    window = int(s.require("cluster.window_hours"))

    row = conn.execute(
        """
        SELECT id, 1 - (centroid <=> %s) AS sim
        FROM clusters
        WHERE status = 'open'
          AND centroid IS NOT NULL
          AND last_seen_at >= %s::timestamptz - make_interval(hours => %s)
          AND first_seen_at <= %s::timestamptz + make_interval(hours => %s)
        ORDER BY centroid <=> %s
        LIMIT 1
        """,
        (embedding, published_at, window, published_at, window, embedding),
    ).fetchone()

    if row is None:
        return None, 0.0
    return row["id"], float(row["sim"])


def _add_to_cluster(conn, cluster_id: int, article_id: int, published_at) -> None:
    """Кладём статью и пересчитываем центроид как среднее по кластеру.

    Среднее считаем в SQL по всем статьям кластера, а не инкрементально:
    так центроид не уплывает от накопленной ошибки и переживает откаты.
    """
    conn.execute("UPDATE articles SET cluster_id = %s WHERE id = %s", (cluster_id, article_id))
    conn.execute(
        """
        UPDATE clusters c SET
            centroid = sub.centroid,
            n_articles = sub.n_articles,
            n_sources = sub.n_sources,
            last_seen_at = GREATEST(c.last_seen_at, %s),
            first_seen_at = LEAST(c.first_seen_at, %s)
        FROM (
            SELECT AVG(embedding) AS centroid,
                   count(*) AS n_articles,
                   count(DISTINCT source_id) AS n_sources
            FROM articles WHERE cluster_id = %s AND embedding IS NOT NULL
        ) sub
        WHERE c.id = %s
        """,
        (published_at, published_at, cluster_id, cluster_id),
    )


def _create_cluster(conn, article_id: int, embedding, published_at) -> int:
    cid = conn.execute(
        """
        INSERT INTO clusters (centroid, first_seen_at, last_seen_at, status, n_articles, n_sources)
        VALUES (%s, %s, %s, 'open', 1, 1)
        RETURNING id
        """,
        (embedding, published_at, published_at),
    ).fetchone()["id"]
    conn.execute("UPDATE articles SET cluster_id = %s WHERE id = %s", (cid, article_id))
    return cid


def run(dry_run: bool = True, limit: int = 5000) -> dict[str, Any]:
    s = get_settings()
    threshold = float(s.require("cluster.sim_threshold"))

    stats: dict[str, Any] = {
        "threshold": threshold,
        "processed": 0,
        "assigned": 0,
        "created": 0,
        "closed": 0,
    }

    with connect() as conn:
        pending = _unclustered(conn, limit)
        stats["pending"] = len(pending)

        if dry_run:
            log.info("DRY-RUN: разложили бы %s статей при пороге %s", len(pending), threshold)
            return stats

        stats["closed"] = close_idle_clusters(conn)

        for art in pending:
            cid, sim = _best_open_cluster(conn, art["embedding"], art["published_at"])
            if cid is not None and sim >= threshold:
                _add_to_cluster(conn, cid, art["id"], art["published_at"])
                stats["assigned"] += 1
            else:
                _create_cluster(conn, art["id"], art["embedding"], art["published_at"])
                stats["created"] += 1
            stats["processed"] += 1

        row = conn.execute(
            "SELECT count(*) AS n FROM clusters WHERE status = 'open'"
        ).fetchone()
        stats["open_clusters"] = row["n"]

    log.info(
        "Кластеризация: обработано %s, в существующие %s, новых %s, закрыто %s",
        stats["processed"], stats["assigned"], stats["created"], stats["closed"],
    )
    return stats
