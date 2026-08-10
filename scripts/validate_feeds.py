#!/usr/bin/env python
"""Проверка фидов перед тем, как класть их в config/sources.yaml.

Для каждого кандидата: доступен ли URL, парсится ли как RSS/Atom, сколько записей
за сутки, есть ли в записях полный текст или только анонс.

    python scripts/validate_feeds.py                      # проверить кандидатов
    python scripts/validate_feeds.py --check-current      # проверить рабочий sources.yaml
    python scripts/validate_feeds.py --write              # записать прошедшие в sources.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import feedparser  # noqa: E402
import yaml  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from quepasa.config import CONFIG_DIR, get_settings, load_sources  # noqa: E402
from quepasa.net import fetch, make_client  # noqa: E402

console = Console(width=int(os.environ.get("QP_TABLE_WIDTH", "170")))

# Ниже этого числа записей за сутки фид бесполезен для ежедневного дайджеста.
MIN_ENTRIES_PER_DAY = 3
# Длина content в записи, начиная с которой считаем, что фид отдаёт полный текст.
FULLTEXT_CHARS = 1500
# Фид, где самая свежая запись старше этого, — брошенный. Отдаёт 200 и валидный XML,
# но новостей в нём нет. Ловим отдельно: иначе такой фид проходит как WARN.
# Официальные источники по своей природе редкие и рваные (Совет министров не заседает
# в августе), поэтому им отдельный порог — иначе они ложно помечаются мёртвыми.
DEAD_FEED_DAYS = {"press": 7, "agency": 7, "official": 45}
# Официальным фидам не предъявляем требование по объёму за сутки.
LOW_VOLUME_OK_TYPES = {"official"}


@dataclass(slots=True)
class FeedReport:
    source_id: str
    name: str
    url: str
    type: str
    lean: str
    status: int = 0
    error: str | None = None
    parsed: bool = False
    feed_kind: str = "-"
    entries_total: int = 0
    entries_24h: int = 0
    has_dates: bool = False
    median_content_chars: int = 0
    content_kind: str = "-"  # full | summary | title-only
    newest_age_hours: float | None = None
    elapsed_ms: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def dead_after_hours(self) -> int:
        return DEAD_FEED_DAYS.get(self.type, 7) * 24

    @property
    def is_dead(self) -> bool:
        return (
            self.newest_age_hours is not None
            and self.newest_age_hours > self.dead_after_hours
        )

    @property
    def verdict(self) -> str:
        if self.error:
            return "FAIL"
        if not self.parsed or self.entries_total == 0:
            return "FAIL"
        if self.is_dead:
            return "FAIL"
        if not self.has_dates:
            return "WARN"
        if self.entries_24h < MIN_ENTRIES_PER_DAY and self.type not in LOW_VOLUME_OK_TYPES:
            return "WARN"
        return "OK"

    @property
    def usable(self) -> bool:
        return self.verdict in ("OK", "WARN") and self.parsed and self.entries_total > 0


def _entry_datetime(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        tm = entry.get(key)
        if tm:
            try:
                return datetime(*tm[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def _entry_content_chars(entry) -> int:
    """Сколько текста реально отдаёт запись фида (без разметки — грубо, по длине)."""
    best = 0
    for block in entry.get("content", []) or []:
        best = max(best, len(block.get("value", "")))
    for key in ("summary", "description"):
        if entry.get(key):
            best = max(best, len(entry[key]))
    return best


def analyse(report: FeedReport, body: str) -> FeedReport:
    parsed = feedparser.parse(body)

    if parsed.bozo and not parsed.entries:
        report.parsed = False
        report.notes.append(f"не парсится: {type(parsed.bozo_exception).__name__}")
        return report

    report.parsed = True
    report.feed_kind = parsed.get("version") or "unknown"
    report.entries_total = len(parsed.entries)
    if parsed.bozo:
        report.notes.append("bozo, но записи есть")

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    dated = 0
    newest: datetime | None = None
    for entry in parsed.entries:
        dt = _entry_datetime(entry)
        if dt is None:
            continue
        dated += 1
        if dt >= cutoff:
            report.entries_24h += 1
        if newest is None or dt > newest:
            newest = dt

    report.has_dates = dated > 0
    if newest is not None:
        report.newest_age_hours = (now - newest).total_seconds() / 3600

    if not report.has_dates:
        report.notes.append("нет дат в записях")
    elif dated < report.entries_total:
        report.notes.append(f"даты у {dated}/{report.entries_total}")

    if report.is_dead:
        report.notes.append(
            f"МЁРТВЫЙ ФИД: свежайшая запись {report.newest_age_hours / 24:.0f} дн. назад"
        )
    elif report.type in LOW_VOLUME_OK_TYPES and report.entries_24h < MIN_ENTRIES_PER_DAY:
        report.notes.append("официальный источник, низкий объём — норма")

    lengths = [_entry_content_chars(e) for e in parsed.entries]
    if lengths:
        report.median_content_chars = int(statistics.median(lengths))
    if report.median_content_chars >= FULLTEXT_CHARS:
        report.content_kind = "full"
    elif report.median_content_chars >= 120:
        report.content_kind = "summary"
    else:
        report.content_kind = "title-only"

    return report


async def check_one(client, source_id, name, url, type_, lean) -> FeedReport:
    report = FeedReport(source_id=source_id, name=name, url=url, type=type_, lean=lean)
    res = await fetch(client, url)
    report.status = res.status
    report.elapsed_ms = res.elapsed_ms

    if res.error:
        report.error = res.error
        return report
    if not res.ok:
        report.error = f"HTTP {res.status}"
        return report

    return analyse(report, res.text)


async def run_candidates(path: Path) -> list[FeedReport]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    jobs = []
    async with make_client() as client:
        sem = asyncio.Semaphore(int(get_settings().get_path("http.max_concurrency", 8)))

        async def guarded(*args):
            async with sem:
                return await check_one(client, *args)

        for cand in raw.get("candidates", []):
            for url in cand["urls"]:
                jobs.append(
                    guarded(cand["id"], cand["name"], url, cand["type"], cand["lean"])
                )
        return await asyncio.gather(*jobs)


async def run_current() -> list[FeedReport]:
    sources = load_sources(include_disabled=True)
    async with make_client() as client:
        sem = asyncio.Semaphore(int(get_settings().get_path("http.max_concurrency", 8)))

        async def guarded(src):
            async with sem:
                return await check_one(client, src.id, src.name, src.feed_url, src.type, src.lean)

        return await asyncio.gather(*[guarded(s) for s in sources if s.feed_url])


def _fmt_age(hours: float | None) -> str:
    if hours is None:
        return "-"
    if hours < 48:
        return f"{hours:.0f} ч"
    return f"{hours / 24:.0f} дн"


async def discover(urls: list[str]) -> None:
    """Ищет <link rel=alternate type=...rss/atom> на страницах изданий.

    Нужно, чтобы не выдумывать URL фидов (§2), когда очевидные варианты отдают 404.
    """
    link_re = re.compile(r"<link[^>]+>", re.I)
    async with make_client() as client:
        for page in urls:
            res = await fetch(client, page)
            if not res.ok:
                console.print(f"[red]{page}: HTTP {res.status} {res.error or ''}[/]")
                continue
            found: list[tuple[str, str]] = []
            for tag in link_re.findall(res.text):
                if not re.search(r'type=["\']application/(rss|atom)\+xml', tag, re.I):
                    continue
                href = re.search(r'href=["\']([^"\']+)["\']', tag, re.I)
                title = re.search(r'title=["\']([^"\']*)["\']', tag, re.I)
                if href:
                    found.append((urljoin(str(res.url), href.group(1)),
                                  title.group(1) if title else ""))
            console.print(f"\n[bold]{page}[/] -> {len(found)} фидов объявлено")
            for href, title in dict.fromkeys(found):
                console.print(f"   {href}   [dim]{title}[/]")


def print_table(reports: list[FeedReport]) -> None:
    table = Table(title="Проверка фидов", show_lines=False)
    table.add_column("verdict", width=7)
    table.add_column("id", style="bold")
    table.add_column("url", overflow="fold", max_width=58)
    table.add_column("HTTP", justify="right", width=5)
    table.add_column("kind", width=9)
    table.add_column("n", justify="right", width=4)
    table.add_column("24h", justify="right", width=4)
    table.add_column("текст", width=10)
    table.add_column("chars", justify="right", width=6)
    table.add_column("возраст", justify="right", width=8)
    table.add_column("заметки", overflow="fold", max_width=34)

    colour = {"OK": "green", "WARN": "yellow", "FAIL": "red"}
    for r in sorted(reports, key=lambda x: (x.source_id, x.verdict)):
        note = "; ".join(r.notes)
        if r.error:
            note = (r.error[:60] + "; " + note).strip("; ")
        table.add_row(
            f"[{colour[r.verdict]}]{r.verdict}[/]",
            r.source_id,
            r.url,
            str(r.status or "-"),
            r.feed_kind,
            str(r.entries_total),
            str(r.entries_24h),
            r.content_kind,
            str(r.median_content_chars),
            _fmt_age(r.newest_age_hours),
            note,
        )
    console.print(table)


def pick_best(reports: list[FeedReport]) -> dict[str, FeedReport]:
    """По одному фиду на издание: сначала verdict, потом свежесть, потом объём текста."""
    rank = {"OK": 0, "WARN": 1, "FAIL": 2}
    best: dict[str, FeedReport] = {}
    for r in reports:
        if not r.usable:
            continue
        cur = best.get(r.source_id)
        key = (rank[r.verdict], -r.entries_24h, -r.median_content_chars)
        if cur is None or key < (rank[cur.verdict], -cur.entries_24h, -cur.median_content_chars):
            best[r.source_id] = r
    return best


def write_sources(reports: list[FeedReport], out_path: Path) -> int:
    best = pick_best(reports)
    all_ids = {r.source_id: r for r in reports}

    entries = []
    for sid in sorted(all_ids, key=lambda s: (s not in best, s)):
        if sid in best:
            r = best[sid]
            entries.append(
                {
                    "id": r.source_id,
                    "name": r.name,
                    "feed_url": r.url,
                    "type": r.type,
                    "lean": r.lean,
                    "weight": 1.0,
                    "lang": "es",
                    "status": "active",
                    "_checked": {
                        "verdict": r.verdict,
                        "entries_24h": r.entries_24h,
                        "content": r.content_kind,
                    },
                }
            )
        else:
            r = all_ids[sid]
            entries.append(
                {
                    "id": r.source_id,
                    "name": r.name,
                    "feed_url": "",
                    "type": r.type,
                    "lean": r.lean,
                    "weight": 1.0,
                    "lang": "es",
                    "status": "disabled",
                    "_reason": "рабочий RSS не найден; HTML не парсим (см. §2 спеки)",
                }
            )

    header = (
        "# Сгенерировано scripts/validate_feeds.py --write.\n"
        "# Только проверенные фиды. lean проставляется вручную владельцем.\n"
        "# Перед правкой прогони проверку: python scripts/validate_feeds.py --check-current\n"
    )
    out_path.write_text(
        header + yaml.safe_dump({"sources": entries}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return len(best)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidates", default=str(CONFIG_DIR / "sources.candidates.yaml"))
    ap.add_argument("--check-current", action="store_true", help="проверить sources.yaml")
    ap.add_argument("--write", action="store_true", help="записать прошедшие в sources.yaml")
    ap.add_argument("--out", default=str(CONFIG_DIR / "sources.yaml"))
    ap.add_argument(
        "--discover",
        nargs="+",
        metavar="URL",
        help="вытащить объявленные фиды со страниц изданий",
    )
    args = ap.parse_args()

    if args.discover:
        asyncio.run(discover(args.discover))
        return 0

    if args.check_current:
        reports = asyncio.run(run_current())
    else:
        reports = asyncio.run(run_candidates(Path(args.candidates)))

    print_table(reports)

    best = pick_best(reports)
    ok = sum(1 for r in best.values() if r.verdict == "OK")
    console.print(
        f"\nИзданий с рабочим фидом: [bold]{len(best)}[/] "
        f"(из них OK: {ok}, WARN: {len(best) - ok}). "
        f"Проверено URL: {len(reports)}."
    )

    if args.write:
        n = write_sources(reports, Path(args.out))
        console.print(f"Записано в [bold]{args.out}[/]: {n} активных источников.")

    if len(best) < 10:
        console.print(
            "[red]Меньше 10 рабочих источников — дальше по спеке идти нельзя (§8.1).[/]"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
