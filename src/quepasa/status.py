"""Живо ли всё. Одна команда, отвечающая «работает или нет» (§6).

Смысл не в красивых цифрах, а в том, чтобы тихая поломка была видна: фиды,
отдающие 200 и пустоту, кончившийся ключ, зависший крон — всё это выглядит
как спокойный новостной день, если не смотреть на свежесть.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .config import get_settings
from .db import connect


def collect() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    with connect() as conn:
        art = conn.execute(
            """
            SELECT count(*) AS total,
                   max(fetched_at) AS last_fetch,
                   count(*) FILTER (WHERE fetched_at >= now() - interval '1 hour') AS last_hour,
                   count(*) FILTER (WHERE fetched_at >= now() - interval '24 hours') AS last_day,
                   count(*) FILTER (WHERE embedding IS NULL) AS no_embedding,
                   count(*) FILTER (WHERE cluster_id IS NULL) AS no_cluster
            FROM articles
            """
        ).fetchone()
        feeds = conn.execute(
            """
            SELECT count(*) FILTER (WHERE status='active') AS active,
                   count(*) FILTER (WHERE status='active' AND (last_ok_at IS NULL
                       OR last_ok_at < now() - interval '3 hours')) AS silent
            FROM sources
            """
        ).fetchone()
        posts = conn.execute(
            """
            SELECT count(*) FILTER (WHERE status='published') AS published,
                   max(published_at) AS last_post,
                   count(*) FILTER (WHERE status='published'
                       AND published_at >= now() - interval '24 hours') AS last_day
            FROM posts
            """
        ).fetchone()
        queue = conn.execute(
            """
            SELECT (SELECT count(*) FROM entity_unresolved) AS unresolved,
                   (SELECT count(*) FROM entities WHERE card_status='draft') AS drafts,
                   (SELECT count(*) FROM post_edits WHERE status='pending') AS edits
            """
        ).fetchone()
        clusters = conn.execute(
            "SELECT count(*) FILTER (WHERE status='open') AS open, "
            "count(*) FILTER (WHERE n_sources>=3) AS big FROM clusters"
        ).fetchone()

    def age_h(ts):
        return None if ts is None else (now - ts).total_seconds() / 3600

    return {
        "articles": dict(art), "feeds": dict(feeds), "posts": dict(posts),
        "queue": dict(queue), "clusters": dict(clusters),
        "fetch_age_h": age_h(art["last_fetch"]),
        "post_age_h": age_h(posts["last_post"]),
    }


def checks(data: dict[str, Any]) -> list[tuple[str, bool, str]]:
    """(что проверяем, в порядке ли, что показать). Порядок — от важного."""
    s = get_settings()
    out: list[tuple[str, bool, str]] = []

    age = data["fetch_age_h"]
    out.append((
        "сбор новостей",
        age is not None and age < 2,
        "ни разу не запускался" if age is None else f"последний сбор {age:.1f} ч назад",
    ))

    f = data["feeds"]
    out.append((
        "фиды",
        f["silent"] <= f["active"] * 0.3,
        f"молчат {f['silent']} из {f['active']} активных",
    ))

    a = data["articles"]
    out.append(("статей за сутки", a["last_day"] > 0, str(a["last_day"])))
    out.append((
        "необработанных",
        a["no_embedding"] == 0 and a["no_cluster"] == 0,
        f"без вектора {a['no_embedding']}, без сюжета {a['no_cluster']}",
    ))

    c = data["clusters"]
    out.append(("сюжетов с ≥3 источниками", c["big"] > 0, str(c["big"])))

    page = data["post_age_h"]
    enabled = bool(s.get_path("autopost.enabled", False))
    out.append((
        "публикация",
        enabled,
        ("выключена: autopost.enabled = false" if not enabled else
         "постов не было" if page is None else f"последний пост {page:.1f} ч назад"),
    ))

    q = data["queue"]
    out.append((
        "ждёт твоего решения",
        True,
        f"сущностей {q['unresolved']}, карточек {q['drafts']}, правок {q['edits']}",
    ))
    return out
