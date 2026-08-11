"""Стадия embed. Эмбеддинг считается один раз на статью и не пересчитывается (§3.4)."""

from __future__ import annotations

import logging
from typing import Any

from ..config import get_settings
from ..db import articles_without_embedding, connect, set_embedding
from ..embed import build_input, embed_texts, plan_batches

log = logging.getLogger(__name__)


def run(dry_run: bool = True, limit: int = 5000) -> dict[str, Any]:
    s = get_settings()
    with connect() as conn:
        pending = articles_without_embedding(conn, limit)

    model_tag = f"{s.require('embed.provider')}/{s.require('embed.model')}"
    stats = {
        "pending": len(pending),
        "embedded": 0,
        "batches": 0,
        "provider": s.require("embed.provider"),
        "model": s.require("embed.model"),
    }
    if not pending:
        log.info("Все статьи уже с эмбеддингами")
        return stats

    if dry_run:
        log.info(
            "DRY-RUN: посчитали бы %s эмбеддингов через %s/%s",
            len(pending), stats["provider"], stats["model"],
        )
        return stats

    all_inputs = [build_input(r["title"], r["text"]) for r in pending]
    batches = plan_batches(all_inputs)
    log.info("Пачек: %s (в среднем по %.0f статей)",
             len(batches), len(pending) / max(1, len(batches)))

    for n, idx in enumerate(batches, 1):
        vectors = embed_texts([all_inputs[i] for i in idx])

        # пишем сразу после каждой пачки: если упрёмся в лимит на середине,
        # посчитанное не пропадёт и следующий запуск продолжит с этого места
        with connect() as conn:
            for i, vec in zip(idx, vectors):
                set_embedding(conn, pending[i]["id"], vec, model_tag)

        stats["embedded"] += len(idx)
        stats["batches"] += 1
        log.info("Эмбеддинги: %s/%s (пачка %s из %s)",
                 stats["embedded"], len(pending), n, len(batches))

    return stats
