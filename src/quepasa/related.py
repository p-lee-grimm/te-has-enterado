"""Связанные сюжеты — строка «Ранее по теме» (§4.2).

Отбор настроен на ТОЧНОСТЬ, не на полноту: одна нелепая связка дискредитирует
механику сильнее, чем десять пропущенных связей помогают. Поэтому близости
центроидов недостаточно — нужно ещё совпадение темы или общая ключевая
сущность, а отклонённая владельцем пара блокируется навсегда.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import get_settings

log = logging.getLogger(__name__)


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def block(conn, a: int, b: int) -> None:
    lo, hi = _pair(a, b)
    conn.execute(
        "INSERT INTO cluster_links (cluster_a, cluster_b, kind, created_by) "
        "VALUES (%s,%s,'blocked','manual') "
        "ON CONFLICT (cluster_a, cluster_b) DO UPDATE SET kind='blocked', "
        "created_by='manual'",
        (lo, hi),
    )


def link(conn, a: int, b: int) -> None:
    """Ручная связь: порог близости не проверяется, человек решил."""
    lo, hi = _pair(a, b)
    conn.execute(
        "INSERT INTO cluster_links (cluster_a, cluster_b, kind, created_by) "
        "VALUES (%s,%s,'related','manual') "
        "ON CONFLICT (cluster_a, cluster_b) DO UPDATE SET kind='related', "
        "created_by='manual'",
        (lo, hi),
    )


def find_related(conn, cluster_id: int, topic: str | None = None,
                 entity_ids: list[str] | None = None) -> dict[str, Any] | None:
    """Один самый близкий сюжет, у которого уже есть пост.

    Возвращает None куда чаще, чем находит, — и это правильное поведение.
    """
    s = get_settings()
    threshold = float(s.get_path("related.sim_threshold", 0.70))
    days = int(s.get_path("related.window_days", 120))

    row = conn.execute(
        """
        WITH me AS (SELECT centroid FROM clusters WHERE id = %(id)s)
        SELECT c.id, c.scope, p.message_id, p.header_md, p.category, p.entity_ids,
               1 - (c.centroid <=> (SELECT centroid FROM me)) AS sim
        FROM clusters c
        JOIN LATERAL (
            SELECT * FROM posts p2
            WHERE p2.cluster_id = c.id AND p2.status = 'published'
              AND p2.message_id IS NOT NULL
            ORDER BY p2.published_at DESC LIMIT 1
        ) p ON TRUE
        WHERE c.id <> %(id)s
          AND c.centroid IS NOT NULL
          AND (SELECT centroid FROM me) IS NOT NULL
          AND p.published_at >= now() - make_interval(days => %(days)s)
          -- пара, отклонённая владельцем, не предлагается больше никогда
          AND NOT EXISTS (
              SELECT 1 FROM cluster_links l
              WHERE l.kind = 'blocked'
                AND l.cluster_a = LEAST(c.id, %(id)s)
                AND l.cluster_b = GREATEST(c.id, %(id)s)
          )
          AND 1 - (c.centroid <=> (SELECT centroid FROM me)) >= %(thr)s
        ORDER BY c.centroid <=> (SELECT centroid FROM me)
        LIMIT 5
        """,
        {"id": cluster_id, "days": days, "thr": threshold},
    ).fetchall()

    mine = set(entity_ids or [])
    for r in row:
        # Главный фильтр точности: близости центроидов мало. Без этого
        # условия порог 0.70 даёт мусор, а он дороже пропуска.
        same_topic = topic and r["category"] and r["category"] == topic
        shared = mine & set(r["entity_ids"] or [])
        if same_topic or shared:
            return {
                "cluster_id": r["id"],
                "message_id": r["message_id"],
                "headline": (r["header_md"] or "").split("\n")[0].strip("* "),
                "sim": float(r["sim"]),
                "why": "тема" if same_topic else "общая сущность",
            }
    return None


def render_link_md(rel: dict[str, Any]) -> str:
    """Строка «Ранее по теме» со ссылкой на прошлый пост канала."""
    from .digest import shorten
    from .telegram import message_link

    url = message_link(rel["message_id"])
    if not url:
        return ""
    title = shorten(rel["headline"], int(
        get_settings().get_path("related.headline_chars", 70)))
    return f"[Ранее по теме: {title}]({url})"
