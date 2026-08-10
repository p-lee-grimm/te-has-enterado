"""Стадия embed. Эмбеддинг считается один раз на статью и не пересчитывается (§3.4)."""

from __future__ import annotations

import logging
from typing import Any

from ..config import get_settings
from ..db import articles_without_embedding, connect, set_embedding
from ..embed import build_input, embed_texts

log = logging.getLogger(__name__)


def run(dry_run: bool = True, limit: int = 5000) -> dict[str, Any]:
    s = get_settings()
    batch_size = int(s.require("embed.batch_size"))

    with connect() as conn:
        pending = articles_without_embedding(conn, limit)

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

    for start in range(0, len(pending), batch_size):
        chunk = pending[start : start + batch_size]
        inputs = [build_input(r["title"], r["text"]) for r in chunk]
        vectors = embed_texts(inputs)

        with connect() as conn:
            for row, vec in zip(chunk, vectors):
                set_embedding(conn, row["id"], vec)

        stats["embedded"] += len(chunk)
        stats["batches"] += 1
        log.info("Эмбеддинги: %s/%s", stats["embedded"], len(pending))

    return stats
