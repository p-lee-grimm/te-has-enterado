"""Контекст прогона и реестр стадий.

Каждая стадия — функция stage(ctx). Она читает то, что положили предыдущие,
кладёт своё и пишет статистику в ctx.stage_stats. Любая может остановить прогон
через ctx.halt() — это штатный исход «сегодня не публикуем» (§0).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import get_settings

log = logging.getLogger(__name__)


@dataclass
class Context:
    dry_run: bool = True
    limit_sources: int | None = None

    run_id: int | None = None
    stage_stats: dict[str, Any] = field(default_factory=dict)

    # то, что стадии передают друг другу
    articles: list[dict] = field(default_factory=list)
    clusters: list[dict] = field(default_factory=list)
    selected: list[dict] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    digest_id: int | None = None

    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0

    halted: bool = False
    halt_reason: str = ""

    def halt(self, reason: str) -> None:
        """Штатная остановка: при аварии не публикуем, а не чиним (§0)."""
        self.halted = True
        self.halt_reason = reason

    def open_run(self) -> None:
        if self.dry_run:
            return
        from .db import connect, start_run

        with connect() as conn:
            self.run_id = start_run(conn)

    def close_run(self, status: str, error: str | None = None) -> None:
        if self.dry_run or self.run_id is None:
            log.info(
                "Итог: %s | стадии: %s",
                status,
                {k: v for k, v in self.stage_stats.items()},
            )
            return
        from .db import connect, finish_run

        if self.halted:
            self.stage_stats["halt_reason"] = self.halt_reason
        with connect() as conn:
            finish_run(
                conn, self.run_id, status, self.stage_stats, error,
                self.tokens_in, self.tokens_out, self.cost_usd,
            )


def _stage_ingest(ctx: Context) -> None:
    from .stages import ingest

    stats, articles = ingest.run(dry_run=ctx.dry_run, limit_sources=ctx.limit_sources)
    ctx.articles = articles
    ctx.stage_stats["ingest"] = stats.as_dict()
    log.info(
        "Фиды: %s/%s ok, статей: %s (новых %s, дублей %s), тексты: %s",
        stats.feeds_ok, stats.feeds_total, stats.entries_seen,
        stats.inserted, stats.dup_url + stats.dup_title, stats.bodies_fetched,
    )
    for f in stats.failures:
        log.warning("  битый фид — %s", f)


def _stage_embed(ctx: Context) -> None:
    from .stages import embed_stage

    stats = embed_stage.run(dry_run=ctx.dry_run)
    ctx.stage_stats["embed"] = stats
    log.info("Эмбеддинги: посчитано %s из %s", stats.get("embedded"), stats.get("pending"))


def _stage_cluster(ctx: Context) -> None:
    from .stages import cluster

    stats = cluster.run(dry_run=ctx.dry_run)
    ctx.stage_stats["cluster"] = stats


def _stage_rank(ctx: Context) -> None:
    from .stages import rank

    stats, ranked = rank.run(dry_run=ctx.dry_run)
    ctx.clusters = ranked
    ctx.stage_stats["rank"] = stats


def _stage_select(ctx: Context) -> None:
    from .stages import select

    if not ctx.clusters:  # стадию запустили отдельно — добираем вход из БД
        _stage_rank(ctx)
    stats, selected = select.run(ctx.clusters, dry_run=ctx.dry_run)
    ctx.selected = selected
    ctx.stage_stats["select"] = stats

    min_items = int(get_settings().require("select.min_items"))
    if len(selected) < min_items:
        ctx.halt(f"отобрано {len(selected)} пунктов при минимуме {min_items}")


def _stage_summarize(ctx: Context) -> None:
    from .stages import select as select_mod
    from .stages import summarize

    if not ctx.selected:
        _stage_select(ctx)
        if ctx.halted:
            return

    stats, items, usage = summarize.run(ctx.selected, dry_run=ctx.dry_run)
    ctx.items = select_mod.enforce_topic_diversity(items)
    ctx.stage_stats["summarize"] = stats
    ctx.tokens_in += usage.tokens_in
    ctx.tokens_out += usage.tokens_out
    ctx.cost_usd += usage.cost_usd


def _stage_render(ctx: Context) -> None:
    from .stages import render

    if not ctx.items:
        _stage_summarize(ctx)
        if ctx.halted:
            return

    stats, messages = render.run(ctx.items)
    ctx.messages = messages
    ctx.stage_stats["render"] = stats


def _stage_gate(ctx: Context) -> None:
    from .stages import gate
    from .telegram import notify_owner

    if not ctx.items:
        _stage_render(ctx)
        if ctx.halted:
            return

    report = gate.run(ctx.items, ctx.stage_stats)
    ctx.stage_stats["gate"] = report.as_dict()

    if not report.passed:
        reason = report.reason()
        ctx.halt(reason)
        if not ctx.dry_run:
            # при аварии не чиним, а не публикуем — и говорим владельцу почему (§0, §3.10)
            notify_owner(f"⚠️ Выпуск не опубликован.\nПричина: {reason}")


def _stage_publish(ctx: Context) -> None:
    from .stages import publish

    if not ctx.messages:
        _stage_render(ctx)
        if ctx.halted:
            return

    stats = publish.run(
        ctx.items, ctx.messages,
        ctx.stage_stats.get("gate"), dry_run=ctx.dry_run,
    )
    ctx.stage_stats["publish"] = stats
    ctx.digest_id = stats.get("digest_id")


STAGE_FUNCS: dict[str, Callable[[Context], None]] = {
    "ingest": _stage_ingest,
    "embed": _stage_embed,
    "cluster": _stage_cluster,
    "rank": _stage_rank,
    "select": _stage_select,
    "summarize": _stage_summarize,
    "render": _stage_render,
    "gate": _stage_gate,
    "publish": _stage_publish,
}
