"""Работа с Postgres. Единственное место, где живёт SQL."""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

from .config import ROOT, env, get_settings

log = logging.getLogger(__name__)

MIGRATIONS_DIR = ROOT / "migrations"


def dsn() -> str:
    return env("DATABASE_URL", required=True)


@contextmanager
def connect(autocommit: bool = False) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(dsn(), row_factory=dict_row, autocommit=autocommit)
    try:
        register_vector(conn)
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


def migrate() -> list[str]:
    """Прогоняет все .sql из migrations по порядку. Файлы обязаны быть идемпотентны."""
    dim = int(get_settings().require("embed.dimensions"))
    applied: list[str] = []
    # register_vector падает, если расширения ещё нет, — первую миграцию гоним без него
    with psycopg.connect(dsn(), autocommit=True) as conn:
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            sql = path.read_text(encoding="utf-8").replace("{{EMBED_DIM}}", str(dim))
            conn.execute(sql)
            applied.append(path.name)
    return applied


def build_vector_index() -> None:
    """ivfflat имеет смысл только на заполненной таблице (§4)."""
    with connect(autocommit=True) as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM articles WHERE embedding IS NOT NULL"
        ).fetchone()["n"]
        if n < 1000:
            log.warning("Статей с эмбеддингом %s — для ivfflat рано, пропускаем", n)
            return
        lists = max(1, min(1000, n // 1000))
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS articles_embedding_idx ON articles "
            f"USING ivfflat (embedding vector_cosine_ops) WITH (lists = {lists})"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS clusters_centroid_idx ON clusters "
            f"USING ivfflat (centroid vector_cosine_ops) WITH (lists = {max(1, lists // 4)})"
        )
        log.info("Векторные индексы построены, lists=%s", lists)


# ---------------------------------------------------------------- sources

def sync_sources(conn: psycopg.Connection) -> int:
    """Переливает config/sources.yaml в таблицу. Конфиг — источник истины."""
    from .config import load_sources

    sources = load_sources(include_disabled=True)
    for s in sources:
        conn.execute(
            """
            INSERT INTO sources
                (id, name, feed_url, type, lean, weight, status, body_fetch, owner_group)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name, feed_url = EXCLUDED.feed_url,
                type = EXCLUDED.type, lean = EXCLUDED.lean,
                weight = EXCLUDED.weight, status = EXCLUDED.status,
                body_fetch = EXCLUDED.body_fetch,
                owner_group = EXCLUDED.owner_group
            """,
            (s.id, s.name, s.feed_url, s.type, s.lean, s.weight, s.status,
             s.body_fetch, s.owner_group),
        )
    return len(sources)


def active_sources(conn: psycopg.Connection) -> list[dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM sources WHERE status = 'active' AND feed_url <> '' ORDER BY id"
    ).fetchall()


def mark_fetch(
    conn: psycopg.Connection,
    source_id: str,
    ok: bool,
    etag: str | None = None,
    last_modified: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE sources SET
            last_fetch_at = now(),
            last_ok_at    = CASE WHEN %s THEN now() ELSE last_ok_at END,
            etag          = COALESCE(%s, etag),
            last_modified = COALESCE(%s, last_modified)
        WHERE id = %s
        """,
        (ok, etag, last_modified, source_id),
    )


# ---------------------------------------------------------------- articles

def insert_article(conn: psycopg.Connection, art: dict[str, Any]) -> int | None:
    """Вставка с дедупом по url_canonical. Возвращает id или None, если дубль."""
    row = conn.execute(
        """
        INSERT INTO articles
            (source_id, url_canonical, url, title, summary_feed, body, body_expires_at,
             title_hash, published_at)
        VALUES (%(source_id)s, %(url_canonical)s, %(url)s, %(title)s, %(summary_feed)s,
                %(body)s, %(body_expires_at)s, %(title_hash)s, %(published_at)s)
        ON CONFLICT (url_canonical) DO NOTHING
        RETURNING id
        """,
        art,
    ).fetchone()
    return row["id"] if row else None


def title_exists(conn: psycopg.Connection, source_id: str, title_hash: str) -> bool:
    """Второй контур дедупа (§3.3): тот же заголовок в том же источнике."""
    row = conn.execute(
        "SELECT 1 FROM articles WHERE source_id = %s AND title_hash = %s LIMIT 1",
        (source_id, title_hash),
    ).fetchone()
    return row is not None


def articles_without_embedding(conn: psycopg.Connection, limit: int) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT id, title, COALESCE(NULLIF(body, ''), summary_feed, '') AS text
        FROM articles
        WHERE embedding IS NULL
        ORDER BY published_at DESC NULLS LAST
        LIMIT %s
        """,
        (limit,),
    ).fetchall()


def set_embedding(
    conn: psycopg.Connection, article_id: int, vec: list[float], model: str
) -> None:
    conn.execute(
        "UPDATE articles SET embedding = %s, embed_model = %s WHERE id = %s",
        (vec, model, article_id),
    )


def embedding_models(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """Какими моделями посчитаны векторы в базе. Больше одной — беда."""
    return conn.execute(
        """
        SELECT COALESCE(embed_model, '(не указана)') AS model, count(*) AS n
        FROM articles WHERE embedding IS NOT NULL
        GROUP BY 1 ORDER BY n DESC
        """
    ).fetchall()


def drop_embeddings(conn: psycopg.Connection, keep_model: str | None = None) -> int:
    """Сбрасывает векторы (и привязку к сюжетам) — при смене модели."""
    row = conn.execute(
        """
        WITH cleared AS (
            UPDATE articles SET embedding = NULL, embed_model = NULL, cluster_id = NULL
            WHERE embedding IS NOT NULL
              AND (%s::text IS NULL OR embed_model IS DISTINCT FROM %s)
            RETURNING 1
        )
        SELECT count(*) AS n FROM cleared
        """,
        (keep_model, keep_model),
    ).fetchone()
    return row["n"]


def purge_expired_bodies(conn: psycopg.Connection) -> int:
    """§5.4 — полный текст живёт не дольше body_retention_hours."""
    row = conn.execute(
        """
        WITH purged AS (
            UPDATE articles SET body = NULL, body_expires_at = NULL
            WHERE body IS NOT NULL AND body_expires_at IS NOT NULL
              AND body_expires_at <= now()
            RETURNING 1
        )
        SELECT count(*) AS n FROM purged
        """
    ).fetchone()
    return row["n"]


def purge_old_embeddings(conn: psycopg.Connection, keep_days: int) -> int:
    """Затирает векторы у статей старше keep_days.

    Вектор нужен, только пока статья участвует в кластеризации (окно 48 ч) и
    в калибровке порога (несколько дней корпуса). Дальше это 4 КБ на строку,
    которые никто не читает, — а именно они и составляют почти весь вес базы.
    Сама статья остаётся: URL, заголовок и наш пересказ храним постоянно (§3.2).
    """
    row = conn.execute(
        """
        WITH purged AS (
            UPDATE articles SET embedding = NULL
            WHERE embedding IS NOT NULL
              AND published_at < now() - make_interval(days => %s)
            RETURNING 1
        )
        SELECT count(*) AS n FROM purged
        """,
        (keep_days,),
    ).fetchone()
    return row["n"]


def daily_article_counts(conn: psycopg.Connection, days: int) -> list[int]:
    """Число статей по суткам за последние N дней — для проверки объёма в gate."""
    rows = conn.execute(
        """
        SELECT date_trunc('day', fetched_at) AS d, count(*) AS n
        FROM articles
        WHERE fetched_at >= now() - make_interval(days => %s)
          AND fetched_at < date_trunc('day', now())
        GROUP BY 1 ORDER BY 1
        """,
        (days,),
    ).fetchall()
    return [r["n"] for r in rows]


# ---------------------------------------------------------------- runs

def start_run(conn: psycopg.Connection) -> int:
    return conn.execute(
        "INSERT INTO runs (status) VALUES ('running') RETURNING id"
    ).fetchone()["id"]


def finish_run(
    conn: psycopg.Connection,
    run_id: int,
    status: str,
    stage_stats: dict[str, Any],
    error: str | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
) -> None:
    conn.execute(
        """
        UPDATE runs SET finished_at = now(), status = %s, stage_stats = %s,
               error = %s, llm_tokens_in = %s, llm_tokens_out = %s, cost_usd = %s
        WHERE id = %s
        """,
        (status, json.dumps(stage_stats, ensure_ascii=False), error,
         tokens_in, tokens_out, round(cost_usd, 4), run_id),
    )


def body_expiry() -> datetime:
    hours = int(get_settings().require("normalize.body_retention_hours"))
    return datetime.now(timezone.utc) + timedelta(hours=hours)
