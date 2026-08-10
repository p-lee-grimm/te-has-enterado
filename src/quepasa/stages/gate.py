"""Стадия gate — ворота качества (§3.10).

Любая непройденная проверка означает: выпуск не публикуется, владельцу уходит
личное сообщение с причиной. Пропущенный выпуск дешевле плохого выпуска и
дешевле ночного хотфикса (§0).
"""

from __future__ import annotations

import asyncio
import logging
import statistics
from dataclasses import dataclass, field
from typing import Any

from ..config import get_settings
from ..db import connect, daily_article_counts
from ..net import Limiter, head_status, make_client
from ..textutil import count_sentences, longest_common_shingle

log = logging.getLogger(__name__)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class GateReport:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append(Check(name, passed, detail))

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    def reason(self) -> str:
        return "; ".join(f"{c.name}: {c.detail}" for c in self.failures)

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks
            ],
        }


async def _check_urls(items: list[dict[str, Any]], report: GateReport) -> None:
    """Все ссылки в посте должны отвечать кодом < 400 (§3.10)."""
    s = get_settings()
    if not s.get_path("gate.url_check_enabled", True):
        return
    max_status = int(s.require("gate.url_check_max_status"))
    tolerated = set(s.get_path("gate.url_check_tolerated_statuses", []) or [])

    urls = [(i, link["url"]) for i, item in enumerate(items) for link in item.get("_links", [])]
    if not urls:
        report.add("ссылки", False, "в выпуске нет ни одной ссылки")
        return

    limiter = Limiter()

    async def check(url: str) -> int:
        async with limiter.slot(url):
            return await head_status(client, url)

    async with make_client() as client:
        statuses = await asyncio.gather(*[check(u) for _, u in urls])

    bad, blocked = [], []
    for (_, url), st in zip(urls, statuses):
        if st in tolerated:
            blocked.append(f"{url} -> {st}")
        elif st == 0 or st >= max_status:
            bad.append(f"{url} -> {st or 'сеть недоступна'}")

    for b in blocked:
        log.warning("  антибот-ответ, ссылку считаем рабочей: %s", b)

    detail = f"проверено {len(urls)}"
    if blocked:
        detail += f", антибот-ответов {len(blocked)} (не считаем провалом)"
    if bad:
        detail = f"нерабочих {len(bad)} из {len(urls)}: {'; '.join(bad[:3])}"

    report.add("ссылки", not bad, detail)


def _check_quotations(items: list[dict[str, Any]], report: GateReport) -> None:
    """Механическая проверка на скрытое цитирование (§3.10, §5.1).

    Скользящее окно по нормализованным словам: совпадение в 10+ слов подряд
    между нашим текстом и исходной статьёй — это цитата, а не пересказ.
    """
    window = int(get_settings().require("gate.plagiarism_window_words"))
    hits: list[str] = []

    for idx, item in enumerate(items, 1):
        sources_text = " ".join(
            (a.get("body") or a.get("summary_feed") or "")
            for a in (item.get("all_articles") or item.get("articles", []))
        )
        for field_name in ("summary", "context", "framing"):
            text = item.get(field_name, "") or ""
            if not text:
                continue
            match = longest_common_shingle(text, sources_text, window)
            if match:
                hits.append(f"пункт {idx}/{field_name}: «{match}»")

    report.add(
        "цитирование",
        not hits,
        f"совпадений {len(hits)}: {'; '.join(hits[:3])}" if hits else
        f"дословных совпадений от {window} слов нет",
    )


def _check_fields(items: list[dict[str, Any]], report: GateReport) -> None:
    s = get_settings()
    max_sentences = int(s.require("gate.max_summary_sentences"))
    problems: list[str] = []

    for idx, item in enumerate(items, 1):
        if not item.get("headline", "").strip():
            problems.append(f"пункт {idx}: пустой заголовок")
        if not item.get("summary", "").strip():
            problems.append(f"пункт {idx}: пустой пересказ")
        elif count_sentences(item["summary"]) > max_sentences:
            problems.append(
                f"пункт {idx}: {count_sentences(item['summary'])} предложений в summary "
                f"при максимуме {max_sentences}"
            )
        if not item.get("context", "").strip():
            problems.append(f"пункт {idx}: пустой контекст")
        if not item.get("_links"):
            problems.append(f"пункт {idx}: нет ни одной ссылки")
        # обрыв на полуслове
        for f in ("summary", "context"):
            val = (item.get(f) or "").rstrip()
            if val and val[-1] not in ".!?…»\"'":
                problems.append(f"пункт {idx}: {f} обрывается на «{val[-25:]}»")

    report.add(
        "поля",
        not problems,
        f"{len(problems)}: {'; '.join(problems[:4])}" if problems else "все поля на месте",
    )


def _check_link_diversity(items: list[dict[str, Any]], report: GateReport) -> None:
    """Ссылки должны вести на разные издания (§3.9)."""
    problems = []
    for idx, item in enumerate(items, 1):
        links = item.get("_links", [])
        if len(links) < 2:
            problems.append(f"пункт {idx}: ссылок {len(links)}")
        elif len({l["source_id"] for l in links}) < len(links):
            problems.append(f"пункт {idx}: ссылки на одно издание")
    report.add(
        "разнообразие ссылок",
        not problems,
        "; ".join(problems[:3]) if problems else "у каждого пункта ≥2 разных издания",
    )


def _check_volume(stage_stats: dict[str, Any], report: GateReport) -> None:
    """Защита от тихой поломки парсинга (§3.10)."""
    s = get_settings()
    days = int(s.require("gate.volume_median_days"))
    min_ratio = float(s.require("gate.min_volume_ratio"))

    ingest = stage_stats.get("ingest", {})
    today = int(ingest.get("inserted", 0))

    with connect() as conn:
        history = daily_article_counts(conn, days)

    if len(history) < 3:
        report.add("объём", True, f"истории всего {len(history)} дн. — проверка пропущена")
        return

    median = statistics.median(history)
    floor = median * min_ratio
    report.add(
        "объём",
        today >= floor,
        f"сегодня {today}, медиана за {len(history)} дн. {median:.0f}, порог {floor:.0f}",
    )


def _check_feeds(stage_stats: dict[str, Any], report: GateReport) -> None:
    min_ratio = float(get_settings().require("gate.min_feed_success_ratio"))
    if "ingest" not in stage_stats:
        # gate запустили отдельной командой — данных о фидах просто нет.
        # Это не провал прогона, а отсутствие входных данных для проверки.
        report.add("фиды", True, "стадия ingest в этом запуске не выполнялась — проверка пропущена")
        return

    ingest = stage_stats["ingest"]
    ratio = float(ingest.get("feed_success_ratio", 0.0))
    report.add(
        "фиды",
        ratio >= min_ratio,
        f"отработало {ratio:.0%} при минимуме {min_ratio:.0%} "
        f"({ingest.get('feeds_ok', 0)}/{ingest.get('feeds_total', 0)})",
    )


def run(items: list[dict[str, Any]], stage_stats: dict[str, Any]) -> GateReport:
    s = get_settings()
    report = GateReport()

    min_items = int(s.require("gate.min_items"))
    report.add(
        "число пунктов",
        len(items) >= min_items,
        f"{len(items)} при минимуме {min_items}",
    )

    _check_feeds(stage_stats, report)
    _check_volume(stage_stats, report)
    _check_fields(items, report)
    _check_link_diversity(items, report)
    _check_quotations(items, report)
    asyncio.run(_check_urls(items, report))

    for c in report.checks:
        log.log(
            logging.INFO if c.passed else logging.ERROR,
            "  [%s] %s — %s", "OK" if c.passed else "СТОП", c.name, c.detail,
        )
    return report
