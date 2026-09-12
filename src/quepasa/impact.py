"""Классификатор значимости: жанр материала, а не тема и не охват.

Отвечает на один вопрос — меняет ли новость что-нибудь для человека,
живущего в Испании, или объясняет ему что-то о стране. Ось независимая
от scope и topic: свидание в Мадриде — spain + soft, решение ЕС
о миграции — world + consequential.

Почему отдельный классификатор, а не штраф в формуле ранжирования: светская
хроника по определению собирает максимальный кросс-спектральный охват — её
печатают все издания независимо от политики, — поэтому метрика широты
консенсуса поднимает её наверх, а не вниз. Штрафовать пришлось бы то самое,
на чём формула держится.

Поле названо impact, а не significance: significance уже занято строкой
в теле поста, которая объясняет читателю, почему новость важна. Здесь речь
о другом — публиковать ли её вообще.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import get_settings, load_prompt
from .db import connect

log = logging.getLogger(__name__)

VALUES = ("consequential", "background", "soft")

# что делать с сюжетом: отдельный пост, строка в «Коротко», не публиковать
POST, DIGEST, DROP = "post", "digest", "drop"


def enabled() -> bool:
    return bool(get_settings().get_path("impact.enabled", False))


def clean(data: dict[str, Any]) -> dict[str, str]:
    """Ответ модели -> вердикт. Невнятное значение считаем background.

    Не soft: молча выбросить новость из-за опечатки модели дороже, чем
    отправить лишнюю строку в дайджест.
    """
    value = str(data.get("impact") or "").strip().lower()
    if value not in VALUES:
        value = "background"
    conf = str(data.get("confidence") or "high").strip().lower()
    return {
        "impact": value,
        "reason": str(data.get("reason") or "").strip()[:300],
        "confidence": conf if conf in ("high", "low") else "high",
    }


def route(verdict: dict[str, Any] | None) -> str:
    """Судьба сюжета по вердикту: POST | DIGEST | DROP.

    Пограничный consequential уходит в дайджест, а не постом: ошибочно
    опубликованный мусор занимает слот и стоит доверия читателя, ошибочно
    не опубликованная новость просто отсутствует и обходится дешевле.
    """
    s = get_settings()
    post_values = set(s.get_path("impact.post_values", ["consequential"]) or [])
    digest_values = set(s.get_path("impact.digest_values", ["background"]) or [])

    if not verdict or not verdict.get("impact"):
        return POST  # вердикта нет — решают прежние правила, а не мы

    value = verdict["impact"]
    if value in post_values:
        if (verdict.get("confidence") == "low"
                and s.get_path("impact.low_confidence_to_digest", True)):
            return DIGEST
        return POST
    if value in digest_values:
        return DIGEST
    return DROP


# ------------------------------------------------------------------ хранилище


def is_fresh(impact_at) -> bool:
    """Не протух ли вердикт. Считается в Python: тот же порог нужен и коду,
    который берёт сюжеты пачкой одним запросом."""
    if impact_at is None:
        return False
    from datetime import datetime, timedelta, timezone

    ttl = float(get_settings().get_path("impact.recheck_after_hours", 24))
    return datetime.now(timezone.utc) - impact_at < timedelta(hours=ttl)


def cached(conn, cluster_id: int) -> dict[str, str] | None:
    """Готовый вердикт сюжета, если он ещё не протух."""
    row = conn.execute(
        "SELECT impact, impact_reason, impact_confidence, impact_at "
        "FROM clusters WHERE id = %s",
        (cluster_id,),
    ).fetchone()
    if not row or not row["impact"] or not is_fresh(row["impact_at"]):
        return None
    return {
        "impact": row["impact"],
        "reason": row["impact_reason"] or "",
        "confidence": row["impact_confidence"] or "high",
    }


def store(conn, cluster_id: int, verdict: dict[str, str]) -> None:
    conn.execute(
        """
        UPDATE clusters SET impact = %s, impact_reason = %s,
               impact_confidence = %s, impact_at = now()
        WHERE id = %s
        """,
        (verdict["impact"], verdict["reason"], verdict["confidence"], cluster_id),
    )


# ------------------------------------------------------------------ вход модели


def cluster_input(conn, cluster_id: int) -> dict[str, Any]:
    """Заголовки, разделы и первый абзац основного материала сюжета."""
    s = get_settings()
    limit = int(s.get_path("impact.max_headlines", 8))
    lead_chars = int(s.get_path("impact.lead_chars", 700))

    rows = conn.execute(
        """
        SELECT DISTINCT ON (a.source_id)
               a.title, a.section, a.body, a.summary_feed,
               s.name AS source_name
        FROM articles a
        JOIN sources s ON s.id = a.source_id
        WHERE a.cluster_id = %s
        ORDER BY a.source_id, (a.body IS NOT NULL) DESC, a.published_at DESC
        """,
        (cluster_id,),
    ).fetchall()

    headlines = [f"- [{r['source_name']}] {r['title']}" for r in rows[:limit]]
    sections = sorted({(r["section"] or "").strip() for r in rows} - {""})
    lead = ""
    for r in rows:
        text = (r["body"] or r["summary_feed"] or "").strip()
        if len(text) > len(lead):
            lead = text
    return {
        "headlines": "\n".join(headlines),
        "sections": ", ".join(sections) or "не определены",
        "lead": lead[:lead_chars],
    }


def user_message(data: dict[str, Any]) -> str:
    return (
        "Заголовки материалов кластера:\n"
        f"{data['headlines']}\n\n"
        f"Разделы изданий, откуда пришли материалы: {data['sections']}\n"
        "Первый абзац основного материала:\n"
        f"{data['lead']}"
    )


def classify(cluster_id: int, *, force: bool = False, usage=None) -> dict[str, str]:
    """Вердикт по сюжету. Готовый берётся из БД, новый считается моделью.

    Сюжет разворачивается: курьёз оказывается поводом для отставки, — поэтому
    вердикт живёт impact.recheck_after_hours, а не вечно.
    """
    from .llm import LLMUsage, json_call

    usage = usage if usage is not None else LLMUsage()

    with connect() as conn:
        if not force:
            hit = cached(conn, cluster_id)
            if hit:
                return hit
        data = cluster_input(conn, cluster_id)

    if not data["headlines"]:
        raise ValueError(f"в сюжете {cluster_id} нет статей")

    verdict = clean(json_call(load_prompt("impact.md"), user_message(data), usage))

    with connect() as conn:
        store(conn, cluster_id, verdict)
    log.info("Сюжет %s: impact=%s (%s) — %s", cluster_id, verdict["impact"],
             verdict["confidence"], verdict["reason"])
    return verdict


def classify_row(row: dict[str, Any], *, usage=None) -> tuple[str, dict[str, str] | None]:
    """(судьба, вердикт) для строки пула сюжетов.

    Обёртка для отбора: классификатор выключен или упал — сюжет идёт прежним
    путём. Отсутствие ответа не должно останавливать канал (§0).
    """
    if not enabled():
        return POST, None

    cid = row.get("cluster_id") or row.get("id")
    if row.get("impact") and is_fresh(row.get("impact_at")):
        verdict = {
            "impact": row["impact"],
            "reason": row.get("impact_reason") or "",
            "confidence": row.get("impact_confidence") or "high",
        }
        return route(verdict), verdict

    try:
        verdict = classify(cid, usage=usage)
    except Exception as exc:  # noqa: BLE001 — один сюжет не роняет прогон
        log.warning("Сюжет %s: значимость не определилась: %s", cid, exc)
        return POST, None
    return route(verdict), verdict


def cached_route(row: dict[str, Any]) -> str | None:
    """Судьба по уже сохранённому вердикту или None, если его нет.

    Нужна отбору в «Коротко»: там сюжетов сотни, и звать модель на каждый
    ради одной строки нельзя. Кто остался без вердикта — получит его позже,
    перед генерацией заголовка.
    """
    if not enabled():
        return None
    if not row.get("impact") or not is_fresh(row.get("impact_at")):
        return None
    return route({
        "impact": row["impact"],
        "confidence": row.get("impact_confidence") or "high",
    })
