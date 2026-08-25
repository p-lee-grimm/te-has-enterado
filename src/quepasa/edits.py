"""Дописывание к опубликованному посту: строки UPD.

Ранние числа и формулировки меняются в течение часов: количество
пострадавших, суммы, статус задержанных. Раньше расхождение приносило
владельцу предложение переписать пост целиком, и до нажатия пост врал.

Теперь пост не переписывается, а дополняется строкой:

    UPD (21:30, El País): погибших 111

Так честнее и дешевле. Читатель видит и исходный текст, и то, что
изменилось, — а тот, кто уже переслал пост в свой чат, не обнаружит,
что текст под ссылкой стал другим. Правка сообщения уведомление не шлёт,
поэтому дописывание ничего не стоит.

Время — по Мадриду: читатель живёт там, а не в UTC.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .config import get_settings, load_prompt
from .db import connect
from .llm import LLMUsage

log = logging.getLogger(__name__)

UPD_PREFIX = "UPD"
# Строка уже дописанного обновления — чтобы не дописать то же дважды.
_UPD_LINE = re.compile(r"^_?UPD \(", re.M)


def _diff_call(published_md: str, titles: list[tuple[str, str]],
               usage: LLMUsage) -> dict[str, Any]:
    from .llm import json_call as _llm_json

    user = (
        f"НАШ ОПУБЛИКОВАННЫЙ ПОСТ:\n{published_md}\n\n"
        "ЗАГОЛОВКИ ИСТОЧНИКОВ СЕЙЧАС:\n"
        + "\n".join(f"- [{src}] {title}" for src, title in titles[:15])
    )
    return _llm_json(load_prompt("fact_diff.md"), user, usage, retries=1)


def candidates(conn) -> list[dict[str, Any]]:
    """Посты в окне дописывания."""
    hours = int(get_settings().get_path("autopost.edit_window_hours", 12))
    return conn.execute(
        """
        SELECT p.*, c.n_articles
        FROM posts p JOIN clusters c ON c.id = p.cluster_id
        WHERE p.status = 'published' AND p.message_id IS NOT NULL
          AND p.published_at >= now() - make_interval(hours => %s)
        """,
        (hours,),
    ).fetchall()


def check_post(conn, post: dict[str, Any]) -> dict[str, Any] | None:
    """Сверяет пост с текущими заголовками. Возвращает {what, source} или None."""
    rows = conn.execute(
        """
        SELECT a.title, s.name AS source
        FROM articles a JOIN sources s ON s.id = a.source_id
        WHERE a.cluster_id = %s ORDER BY a.published_at DESC LIMIT 15
        """,
        (post["cluster_id"],),
    ).fetchall()
    if not rows:
        return None
    pairs = [(r["source"], r["title"]) for r in rows]
    titles = [t for _, t in pairs]

    usage = LLMUsage()
    try:
        data = _diff_call(post["header_md"], pairs, usage)
    except Exception as exc:  # noqa: BLE001 — один пост не роняет прогон
        log.warning("Сверка фактов по посту %s не удалась: %s", post["id"], exc)
        return None

    if not data.get("changed"):
        return None

    # Те же правила имён, что и при сборке поста: приписка идёт отдельным
    # вызовом модели, и без этого «ПП» и транскрипции возвращаются в уже
    # вычищенный пост.
    from .posts import fix_names, restore_latin_names

    what = restore_latin_names(fix_names((data.get("what") or "").strip()), titles)
    if not what:
        return None

    source = (data.get("source") or "").strip()
    known = {r["source"] for r in rows}
    if source not in known:
        # издание, которого нет в списке, — признак выдумки; берём свежайшее
        source = rows[0]["source"]

    return {"post_id": post["id"], "what": what.rstrip("."), "source": source,
            "cost_usd": round(usage.cost_usd, 4)}


def already_said(header_md: str, what: str) -> bool:
    """Это же обновление уже дописано.

    Источники повторяют новое число во всех заголовках сутки подряд, и без
    этой проверки под постом выросла бы колонка одинаковых UPD.
    """
    from .textutil import normalize_words

    target = " ".join(normalize_words(what))
    for line in (header_md or "").split("\n"):
        if _UPD_LINE.match(line.strip()) and target in " ".join(normalize_words(line)):
            return True
    return False


def apply_update(conn, post: dict[str, Any], upd: dict[str, Any]) -> bool:
    """Дописывает строку UPD к посту и пересобирает сообщение в канале."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from .posts import refresh_one

    if already_said(post["header_md"], upd["what"]):
        return False

    tz = ZoneInfo(get_settings().require("render.timezone"))
    stamp = datetime.now(tz).strftime("%H:%M")
    line = f"_{UPD_PREFIX} ({stamp}, {upd['source']}): {upd['what']}_"
    header = f"{post['header_md']}\n\n{line}"

    conn.execute("UPDATE posts SET header_md = %s WHERE id = %s",
                 (header, post["id"]))
    ok = refresh_one(post["id"])
    if not ok:
        # текст в канал не ушёл — не держим приписку в базе, иначе следующая
        # сверка сочтёт её уже сделанной
        conn.execute("UPDATE posts SET header_md = %s WHERE id = %s",
                     (post["header_md"], post["id"]))
        return False
    log.info("Пост %s дополнен: %s (%s)", post["id"], upd["what"], upd["source"])
    return True


def run(dry_run: bool = True) -> dict[str, Any]:
    """Сверяет посты в окне и дописывает к ним UPD."""
    stats = {"checked": 0, "changed": 0, "applied": 0, "cost_usd": 0.0}
    with connect() as conn:
        posts_ = candidates(conn)
    stats["checked"] = len(posts_)

    for post in posts_:
        with connect() as conn:
            upd = check_post(conn, post)
            if upd is None:
                continue
            stats["changed"] += 1
            stats["cost_usd"] += upd["cost_usd"]
            if dry_run:
                log.info("DRY-RUN: пост %s — UPD (%s): %s",
                         post["id"], upd["source"], upd["what"])
                continue
            if apply_update(conn, post, upd):
                stats["applied"] += 1

    log.info("Сверка фактов: проверено %s, расхождений %s, дописано %s",
             stats["checked"], stats["changed"], stats["applied"])
    return stats
