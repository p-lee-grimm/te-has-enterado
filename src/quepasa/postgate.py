"""Ворота качества для отдельного поста (§8 спеки контекстного слоя).

Проверяются перед каждой публикацией. Непройденная проверка означает, что
этот пост не выходит, а причина уходит в чат ревью. Остальные посты прогона
это не блокирует: одна плохая карточка не должна останавливать канал.

Отличие от stages/gate.py: тот проверяет дневной выпуск целиком, этот —
единичный пост. Логика решения общая, поэтому отчёт тот же.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from .config import get_settings
from .db import connect
from .net import Limiter, head_status, make_client
from .stages.gate import Check, GateReport  # общий формат отчёта
from .textutil import count_sentences, longest_common_shingle

log = logging.getLogger(__name__)

__all__ = ["Check", "GateReport", "check_post"]


def _check_rule(report: GateReport, sources: list[dict[str, Any]]) -> None:
    from .spectrum import n_owners, passes_rule, political_sources

    ok = passes_rule(sources)
    report.add(
        "ветки допуска",
        ok,
        f"владельцев без агентств: {n_owners(political_sources(sources))}"
        if ok else "не выполнена ни одна из трёх веток",
    )


def _check_scope(report: GateReport, scope: str | None) -> None:
    """world не получает отдельного поста никогда — только строку в «Коротко»."""
    if not scope:
        report.add("scope", True, "не определён — проверка пропущена")
        return
    report.add("scope", scope != "world", f"{scope}")


def _check_geo_tag(report: GateReport, geo_tag: str | None) -> None:
    from .geo import known_tags

    if not geo_tag:
        report.add("гео-тег", True, "нет — это нормально")
        return
    tags = known_tags()
    report.add(
        "гео-тег",
        geo_tag in tags,
        geo_tag if geo_tag in tags else f"{geo_tag} нет в config/geo_tags.yaml",
    )


def _check_one_sided_line(report: GateReport, one_sided: bool, body_md: str) -> None:
    from .posts import ONE_SIDED_LINE

    if not one_sided:
        report.add("односторонность", True, "сюжет не односторонний")
        return
    marker = ONE_SIDED_LINE.strip("_")
    report.add(
        "односторонность",
        marker in body_md,
        "строка на месте" if marker in body_md else "нет строки об одном лагере",
    )


# Глаголы речи: заголовок с ними сообщает, что разговор состоялся, а не что
# в нём сказано. «Vivas ответил на заявление министра Марокко о Сеуте» —
# прочитав это, читатель знает ровно столько же, сколько до заголовка,
# хотя источники прямо цитировали суть: «Ceuta es España».
_SPEECH_ACT = re.compile(
    r"\b(ответил\w*|отреагировал\w*|прокомментировал\w*|высказал\w*ся|"
    r"выступил\w*\s+с\s+заявлением|обратил\w*ся)\b", re.I
)
# Двоеточие, кавычка, тире или «что» — признак того, что суть всё-таки
# в заголовке: «Vivas ответил: Сеута — это Испания».
_HAS_SUBSTANCE = re.compile(r"[:«\"—]|\bчто\b", re.I)


def _check_headline_substance(report: GateReport, headline: str, summary: str) -> None:
    """Заголовок про сам факт высказывания без пояснения — пустой пост."""
    bare = bool(_SPEECH_ACT.search(headline)) and not _HAS_SUBSTANCE.search(headline)
    ok = not bare or bool((summary or "").strip())
    report.add(
        "содержательность",
        ok,
        "в заголовке сказано, что именно" if ok
        else "заголовок сообщает о факте высказывания, а не о его сути, и нет lead",
    )


def _check_fields(report: GateReport, headline: str, summary: str,
                  significance: str) -> None:
    s = get_settings()
    max_sentences = int(s.get_path("gate.max_summary_sentences", 4))
    problems: list[str] = []

    if not headline.strip():
        problems.append("пустой заголовок")
    elif headline.rstrip().endswith("."):
        # заголовок точкой не заканчивается — это правило промпта
        problems.append("заголовок с точкой в конце")
    elif len(headline) > int(s.get_path("posts.max_headline_chars", 120)):
        problems.append(f"заголовок {len(headline)} символов")

    # Пустые lead и significance разрешены: если заголовок объясняет всё,
    # выжимать из модели лишнее предложение вредно. Требуем только,
    # чтобы непустое не было обрезано на полуслове.
    if summary.strip() and count_sentences(summary) > max_sentences:
        problems.append(
            f"{count_sentences(summary)} предложений при максимуме {max_sentences}"
        )
    if s.get_path("posts.require_significance", False) and not significance.strip():
        problems.append("пустой significance")

    # Обрыв ищем только в пересказе, и только если он многословный.
    #
    # significance — это одна фраза, и точка в конце для неё необязательна:
    # требовать её значит блокировать нормальные посты. Настоящий обрыв
    # (модель упёрлась в max_tokens) сюда всё равно не доедет — оборванный
    # ответ не разберётся как JSON ещё на шаг раньше.
    v = (summary or "").rstrip()
    if v and count_sentences(v) > 1 and v[-1] not in ".!?…»\"'%)":
        problems.append(f"пересказ обрывается на «{v[-25:]}»")

    report.add("поля", not problems, "; ".join(problems) if problems else "заполнены")


def _check_quotations(report: GateReport, texts: list[str],
                      articles: list[dict[str, Any]]) -> None:
    """Механическая проверка на скрытое цитирование (§5.1 основной спеки)."""
    window = int(get_settings().require("gate.plagiarism_window_words"))
    source_text = " ".join(
        (a.get("body") or a.get("summary_feed") or "") for a in articles
    )
    hits = []
    for text in texts:
        if not text:
            continue
        match = longest_common_shingle(text, source_text, window)
        if match:
            hits.append(f"«{match}»")
    report.add(
        "цитирование",
        not hits,
        "; ".join(hits[:2]) if hits else f"совпадений от {window} слов нет",
    )


def _check_recent_publication(report: GateReport, cluster_id: int) -> None:
    hours = int(get_settings().get_path("autopost.repeat_min_hours", 12))
    with connect() as conn:
        row = conn.execute(
            """
            SELECT EXTRACT(EPOCH FROM (now() - published_at)) / 3600 AS h
            FROM posts
            WHERE cluster_id = %s AND status = 'published'
            ORDER BY published_at DESC LIMIT 1
            """,
            (cluster_id,),
        ).fetchone()
    if row is None or row["h"] is None:
        report.add("повтор", True, "сюжет ещё не публиковался")
        return
    h = float(row["h"])
    report.add("повтор", h >= hours, f"прошлый пост {h:.1f} ч назад, нужно ≥{hours}")


async def _check_urls_async(report: GateReport, articles: list[dict[str, Any]]) -> None:
    s = get_settings()
    if not s.get_path("gate.url_check_enabled", True):
        return
    max_status = int(s.require("gate.url_check_max_status"))
    tolerated = set(s.get_path("gate.url_check_tolerated_statuses", []) or [])

    urls = [a.get("url") or a.get("url_canonical") for a in articles]
    urls = [u for u in urls if u]
    if not urls:
        report.add("ссылки", False, "у поста нет ни одной ссылки")
        return

    limiter = Limiter()

    async def one(client, url):
        async with limiter.slot(url):
            return await head_status(client, url)

    async with make_client() as client:
        statuses = await asyncio.gather(*[one(client, u) for u in urls])

    bad = [f"{u} -> {st or 'сеть'}" for u, st in zip(urls, statuses)
           if st not in tolerated and (st == 0 or st >= max_status)]
    report.add(
        "ссылки",
        not bad,
        f"нерабочих {len(bad)}: {'; '.join(bad[:2])}" if bad else f"проверено {len(urls)}",
    )


def check_post(
    *,
    cluster_id: int,
    headline: str,
    summary: str = "",
    significance: str = "",
    body_md: str = "",
    articles: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    one_sided: bool = False,
    scope: str | None = None,
    geo_tag: str | None = None,
    check_urls: bool = True,
) -> GateReport:
    """Полная проверка одного поста. Ничего не публикует и не пишет в БД."""
    report = GateReport()

    _check_rule(report, sources)
    _check_scope(report, scope)
    _check_geo_tag(report, geo_tag)
    _check_one_sided_line(report, one_sided, body_md)
    _check_fields(report, headline, summary, significance)
    _check_headline_substance(report, headline, summary)
    _check_quotations(report, [headline, summary, significance], articles)
    _check_recent_publication(report, cluster_id)
    if check_urls:
        asyncio.run(_check_urls_async(report, articles))

    for c in report.checks:
        log.log(
            logging.INFO if c.passed else logging.WARNING,
            "  [%s] %s — %s", "ok" if c.passed else "СТОП", c.name, c.detail,
        )
    return report
