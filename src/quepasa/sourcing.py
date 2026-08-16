"""Лестница источников для фактов: от энциклопедии к прессе.

Права убывают вместе с уровнем. Википедия разрешает всё, Wikidata — только
каркас, официальный сайт — каркас без характеристики (ни одна партия
не назовёт себя ультраправой), пресса — должность при имени и оценку
с именем издания.

Если ни один источник не нашёлся — пула нет, показывается только role_gloss.
Это штатный исход, а не сбой.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import get_settings
from .net import user_agent
from .wordlists import normalize

log = logging.getLogger(__name__)

# Порядок языков: испанский, затем языки автономных сообществ, затем
# английский. Про испанского регионального политика статья на каталанском
# или галисийском есть заметно чаще, чем на английском.
WIKI_LANGS = ("es", "ca", "gl", "eu", "en")

TIMEOUT = 30


def _summary(title: str, lang: str) -> dict[str, Any] | None:
    """Вводная секция статьи. Недоступность Википедии — не отсутствие статьи,
    но и не повод ронять извлечение: идём дальше по лестнице."""
    from . import wiki

    try:
        return wiki.summary(title, lang=lang)
    except wiki.TransientError as exc:
        log.warning("Википедия (%s) недоступна на «%s»: %s", lang, title, exc)
        return None


def _own_article(name: str, url: str | None, context: str) -> dict[str, Any] | None:
    """Статья о самой сущности: точный URL, иначе поиск по языкам."""
    from . import wiki

    if url:
        try:
            got = wiki.fetch_for_entity(name, url, context)
        except wiki.TransientError as exc:
            log.warning("Статья по ссылке %s не открылась: %s", url, exc)
            got = None
        if got and got.get("extract"):
            return got

    for lang in WIKI_LANGS:
        hits = wiki.search(name, lang=lang)
        if not hits:
            continue
        got = _summary(hits[0]["title"], lang)
        if got and got.get("extract"):
            got["resolved_by"] = "search"
            got["candidates"] = hits[:5]
            return got
    return None


def _org_article(name: str) -> dict[str, Any] | None:
    """Статья об организации, в которой сущность названа.

    Правило подстановки сущности: у половины министров и почти у всех
    журналистов своей статьи нет, а у ведомства или канала — есть, и в ней
    человек назван по имени. Организация становится носителем контекста,
    а роль персоны записывается фактом со ссылкой на эту статью.

    Проверка механическая: имя (фамилия) обязано встречаться в тексте статьи.
    Без неё поиск подставил бы первую попавшуюся организацию.
    """
    from . import wiki

    surname = (normalize(name).split() or [""])[-1]
    if len(surname) < 4:
        return None

    for lang in WIKI_LANGS:
        for hit in wiki.search(name, lang=lang, limit=5):
            if normalize(hit["title"]) == normalize(name):
                continue  # это статья о самой персоне, её берёт _own_article
            got = _summary(hit["title"], lang)
            if not got or not got.get("extract"):
                continue
            if surname in normalize(got["extract"]).split():
                log.info("Носителем контекста для «%s» стала статья «%s»",
                         name, got["title"])
                return got
    return None


def _wikidata(name: str) -> dict[str, Any] | None:
    """Запись Wikidata: описание в одну строку.

    Тонкий источник, но он закрывает случай «статьи нет ни на одном языке,
    а запись есть»: должность и род занятий в описании обычно стоят.
    """
    try:
        r = httpx.get(
            "https://www.wikidata.org/w/api.php",
            params={"action": "wbsearchentities", "search": name, "language": "es",
                    "uselang": "es", "format": "json", "limit": 1},
            headers={"User-Agent": user_agent()},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        items = r.json().get("search") or []
    except Exception as exc:  # noqa: BLE001 — фолбэк не должен ронять извлечение
        log.warning("Wikidata не ответила по «%s»: %s", name, exc)
        return None

    if not items:
        return None
    item = items[0]
    text = " — ".join(x for x in (item.get("label"), item.get("description")) if x)
    if not (item.get("description") or "").strip():
        return None
    return {"title": item.get("label") or name, "extract": text,
            "url": f"https://www.wikidata.org/wiki/{item['id']}", "lang": "es"}


def _press(conn, entity: dict[str, Any], limit: int = 6) -> list[dict[str, Any]]:
    """Статьи, где имя встретилось, по одному источнику на запись.

    Полюс источника едет вместе с текстом: спорность характеристики решается
    тем, сходятся ли разные бакеты, и без метки полюса это решение не принять.
    """
    import re

    from .spectrum import bucket

    name = entity["name_es"]
    # по границам слова: ILIKE '%INE%' находит «cine» и «Medicine»
    pattern = r"\y" + re.escape(name) + r"\y"
    rows = conn.execute(
        """
        SELECT a.title, coalesce(a.body, a.summary_feed, '') AS text,
               coalesce(a.url, a.url_canonical) AS url,
               s.name AS source_name, s.lean, s.type
        FROM articles a JOIN sources s ON s.id = a.source_id
        WHERE (a.title ~* %s OR a.summary_feed ~* %s OR a.body ~* %s)
        ORDER BY a.published_at DESC NULLS LAST
        LIMIT %s
        """,
        (pattern, pattern, pattern, limit),
    ).fetchall()

    out = []
    for r in rows:
        if not (r["text"] or "").strip():
            continue
        out.append({
            "tier": "press",
            "url": r["url"],
            "title": r["title"],
            "text": f"[{r['source_name']}] {r['title']}\n{r['text']}",
            "attribution": r["source_name"],
            "bucket": bucket(r["lean"]) or "" if r["type"] == "press" else "",
        })
    return out


def ladder(conn, entity: dict[str, Any], *, with_press: bool = True
           ) -> list[dict[str, Any]]:
    """Все источники сущности по убыванию прав.

    Возвращает список словарей `{tier, url, text, bucket, title}`. Пустой
    список означает, что фактов не будет вовсе, — и это нормальный исход.
    """
    name = entity["name_es"]
    out: list[dict[str, Any]] = []

    own = _own_article(name, entity.get("wiki_url_es") or entity.get("wiki_url_ru"),
                       entity.get("name_ru", ""))
    if own:
        out.append({"tier": "wiki", "url": own["url"], "title": own["title"],
                    "text": own["extract"], "bucket": ""})
    else:
        org = _org_article(name)
        if org:
            out.append({"tier": "wiki_org", "url": org["url"], "title": org["title"],
                        "text": org["extract"], "bucket": ""})

    if not out:
        wd = _wikidata(name)
        if wd:
            out.append({"tier": "wikidata", "url": wd["url"], "title": wd["title"],
                        "text": wd["extract"], "bucket": ""})

    official = (entity.get("official_url") or "").strip()
    if official:
        text = _fetch_text(official)
        if text:
            out.append({"tier": "official", "url": official, "title": name,
                        "text": text, "bucket": ""})

    if with_press:
        out.extend(_press(conn, entity))

    log.info("Источники для %s: %s", entity["id"],
             ", ".join(f"{s['tier']}" for s in out) or "нет")
    return out


def _fetch_text(url: str) -> str:
    """Текст официальной страницы. Ошибка — не повод ронять извлечение."""
    from .textutil import strip_html

    try:
        r = httpx.get(url, headers={"User-Agent": user_agent()}, timeout=TIMEOUT,
                      follow_redirects=True)
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.warning("Официальная страница %s не открылась: %s", url, exc)
        return ""
    limit = int(get_settings().get_path("facts.max_source_chars", 6000))
    return strip_html(r.text)[:limit]
