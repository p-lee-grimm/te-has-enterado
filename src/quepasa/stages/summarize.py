"""Стадия summarize — один вызов LLM на кластер (§3.8)."""

from __future__ import annotations

import logging
from typing import Any

from ..config import get_settings, load_prompt
from ..llm import LLMError, LLMUsage, summarize_call

log = logging.getLogger(__name__)

LEAN_RU = {
    "left": "левое",
    "center-left": "левоцентристское",
    "center": "центристское",
    "center-right": "правоцентристское",
    "right": "правое",
}

TYPE_RU = {"press": "издание", "agency": "агентство", "official": "официальный источник"}


def build_user_message(item: dict[str, Any]) -> str:
    """Собирает вход для модели: тексты разных изданий с их полюсами."""
    s = get_settings()
    max_chars = int(s.require("summarize.max_chars_per_article"))

    parts: list[str] = []
    if item.get("is_continuation") and item.get("previous_summary"):
        parts.append(
            "ЭТО ПРОДОЛЖЕНИЕ СЮЖЕТА. Наш предыдущий пересказ:\n"
            f"{item['previous_summary']}\n\n"
            "Ниже свежие материалы. Напиши, что изменилось с прошлого раза.\n"
        )

    parts.append(f"Материалов об одном событии: {len(item['articles'])}\n")

    for i, art in enumerate(item["articles"], 1):
        text = (art.get("body") or art.get("summary_feed") or "").strip()[:max_chars]
        lean = LEAN_RU.get(art["lean"], art["lean"])
        kind = TYPE_RU.get(art["type"], art["type"])
        parts.append(
            f"--- Материал {i} ---\n"
            f"Издание: {art['source_name']} ({kind}, полюс: {lean})\n"
            f"Заголовок: {art['title']}\n"
            f"Текст: {text or '(полный текст недоступен, есть только заголовок)'}\n"
        )

    return "\n".join(parts)


def run(selected: list[dict[str, Any]], dry_run: bool = True) -> tuple[dict, list[dict], LLMUsage]:
    system = load_prompt("summarize.md")
    usage = LLMUsage()
    stats = {"clusters_in": len(selected), "summarized": 0, "dropped": 0}

    if dry_run:
        log.info(
            "DRY-RUN: %s вызовов LLM к %s не делаем",
            len(selected), get_settings().require("summarize.model"),
        )
        for item in selected:
            item.setdefault("headline", f"[dry-run] кластер {item['cluster_id']}")
            item.setdefault("summary", "")
            item.setdefault("context", "")
            item.setdefault("framing", "")
            item.setdefault("topic", "")
            item.setdefault("confidence", "high")
        return stats, selected, usage

    out: list[dict[str, Any]] = []
    for item in selected:
        try:
            result = summarize_call(system, build_user_message(item), usage)
        except LLMError as exc:
            # кластер выбрасывается из выпуска, прогон продолжается (§3.8)
            log.warning("Кластер %s выброшен: %s", item["cluster_id"], exc)
            stats["dropped"] += 1
            continue

        item.update(result.model_dump())
        out.append(item)
        stats["summarized"] += 1

    stats["tokens_in"] = usage.tokens_in
    stats["tokens_out"] = usage.tokens_out
    stats["cost_usd"] = round(usage.cost_usd, 4)
    log.info(
        "Пересказано %s кластеров, выброшено %s, токенов %s/%s, $%.4f",
        stats["summarized"], stats["dropped"],
        usage.tokens_in, usage.tokens_out, usage.cost_usd,
    )
    return stats, out, usage
