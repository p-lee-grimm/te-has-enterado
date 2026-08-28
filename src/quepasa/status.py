"""Живо ли всё. Одна команда, отвечающая «работает или нет» (§6).

Смысл не в красивых цифрах, а в том, чтобы тихая поломка была видна: фиды,
отдающие 200 и пустоту, кончившийся ключ, зависший крон — всё это выглядит
как спокойный новостной день, если не смотреть на свежесть.
"""

from __future__ import annotations

import logging

from datetime import datetime, timezone
from typing import Any

from .db import connect

log = logging.getLogger(__name__)


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
                   (SELECT count(*) FROM entities e WHERE NOT e.never_explain
                     AND NOT EXISTS (SELECT 1 FROM entity_facts f
                                     WHERE f.entity_id = e.id AND f.status='active'))
                       AS drafts,
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
    from .posts import autopost_enabled

    from .config import get_settings

    enabled = autopost_enabled()
    # Включённого переключателя мало: трое суток простоя эта проверка
    # показывала как «в порядке», потому что смотрела только на него.
    # К порогу тишины прибавляем ночь: окно закрыто с 21 до 9, и утром
    # свежесть в 12 часов — норма, а не поломка.
    limit = float(get_settings().get_path("autopost.silence_alert_hours", 6)) + 12
    fresh = page is not None and page <= limit
    out.append((
        "публикация",
        enabled and fresh,
        ("выключена: autopost.enabled = false" if not enabled else
         "постов не было ни разу" if page is None else
         f"последний пост {page:.1f} ч назад, а порог {limit:.0f}" if not fresh else
         f"последний пост {page:.1f} ч назад"),
    ))

    q = data["queue"]
    out.append((
        "ждёт твоего решения",
        True,
        f"сущностей {q['unresolved']}, без пула фактов {q['drafts']}, "
        f"правок {q['edits']}",
    ))
    return out


# ------------------------------------------------------------------ сторож

WATCH_KEY = "silence_alerted_at"


def watch_silence(now=None) -> str | None:
    """Сообщает владельцу, если канал молчит внутри окна публикации.

    Три дня без постов прошли незамеченными: автопостинг не запускался,
    потому что зависший прогон держал лок, а узнать об этом было неоткуда —
    статус смотрят руками, а руками его никто не смотрит.

    Сообщение шлётся один раз в сутки: сторож, который пишет каждые пять
    минут, перестаёт читаться на второй час.
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    from .config import get_settings
    from .db import connect
    from .telegram import notify_owner

    s = get_settings()
    limit = float(s.get_path("autopost.silence_alert_hours", 6))
    tz = ZoneInfo(s.require("render.timezone"))
    # время параметром: иначе проверку окна не протестировать, а именно
    # в ней легче всего ошибиться на час
    now = now or _dt.now(tz)

    lo = int(s.get_path("autopost.window_from_hour", 9))
    hi = int(s.get_path("autopost.window_to_hour", 21))
    # вне окна тишина штатная, и будить из-за неё нельзя
    if not (lo <= now.hour < hi):
        return None

    with connect() as conn:
        row = conn.execute(
            """
            SELECT EXTRACT(EPOCH FROM (now() - max(published_at))) / 3600 AS h
            FROM posts WHERE status = 'published'
            """
        ).fetchone()
        age = float(row["h"]) if row and row["h"] is not None else None
        if age is None or age < limit:
            return None

        last = conn.execute(
            "SELECT value FROM bot_state WHERE key = %s", (WATCH_KEY,)
        ).fetchone()
        if last:
            since = (now - _dt.fromisoformat(last["value"])).total_seconds()
            if since < 24 * 3600:
                return None

        conn.execute(
            "INSERT INTO bot_state (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (WATCH_KEY, now.isoformat()),
        )

    text = (f"⚠️ <b>Канал молчит {age:.0f} ч</b>\n\n"
            f"Сейчас окно публикации, а постов нет. Проверь /status — "
            f"обычно это зависший прогон, который держит лок.")
    notify_owner(text)
    log.warning("Сторож: постов нет %.0f ч", age)
    return text
