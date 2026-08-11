"""Википедия — единственный источник фактов для карточек (§7.4).

Модель не имеет права добавлять факты от себя: ей отдаётся вводная секция
статьи и запрещается выходить за её пределы. Проверка на это — отдельным
дешёвым вызовом на этапе валидации.

Тексты Википедии под CC BY-SA, поэтому ссылка на статью-источник обязательна
и хранится вместе с сущностью (§11).
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote, unquote

import httpx

from .net import user_agent

log = logging.getLogger(__name__)

TIMEOUT = 30
# Уточнение к имени при поиске. Длиннее — поиск начинает отвечать 500,
# и вместо сужения получается отказ.
_MAX_HINT = 60


def _api(lang: str) -> str:
    return f"https://{lang}.wikipedia.org"


def search(query: str, lang: str = "es", limit: int = 5) -> list[dict[str, Any]]:
    """Ищет статью по имени. Возвращает кандидатов, выбирает человек."""
    try:
        r = httpx.get(
            f"{_api(lang)}/w/rest.php/v1/search/page",
            params={"q": query, "limit": limit},
            headers={"User-Agent": user_agent()},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return [
            {"title": p["title"], "description": p.get("description") or "",
             "key": p["key"]}
            for p in r.json().get("pages", [])
        ]
    except Exception as exc:  # noqa: BLE001 — поиск не должен ронять команду
        log.warning("Поиск в Википедии (%s) не удался: %s", lang, exc)
        return []


class TransientError(RuntimeError):
    """Временная ошибка Википедии: 429/5xx или сеть.

    Отличать её от «статьи нет» обязательно: иначе разовый 403 молча
    превращает точный URL в поиск по имени, а поиск — в однофамильца.
    """


def summary(title: str, lang: str = "es") -> dict[str, Any] | None:
    """Вводная секция статьи и канонический URL.

    Именно вводная, а не вся статья: в ней по устройству Википедии лежит
    ответ на «кто это», а остальное только запутает модель.
    """
    try:
        r = httpx.get(
            f"{_api(lang)}/api/rest_v1/page/summary/{quote(title, safe='')}",
            headers={"User-Agent": user_agent()},
            timeout=TIMEOUT,
            follow_redirects=True,
        )
        if r.status_code == 404:
            return None
        if r.status_code in (403, 429) or r.status_code >= 500:
            raise TransientError(f"HTTP {r.status_code}")
        r.raise_for_status()
        d = r.json()
        if d.get("type") == "disambiguation":
            log.warning("«%s» — страница неоднозначности, нужен точный заголовок", title)
            return None
        return {
            "title": d.get("title", title),
            "extract": (d.get("extract") or "").strip(),
            "url": (d.get("content_urls", {}).get("desktop", {}) or {}).get("page")
                   or f"{_api(lang)}/wiki/{quote(title)}",
            "description": d.get("description") or "",
            "lang": lang,
        }
    except TransientError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось получить статью «%s» (%s): %s", title, lang, exc)
        return None


def _with_retry(fn, attempts: int = 3, wait: float = 2.0):
    """Повтор на временных ошибках. Точный URL не должен вырождаться в поиск."""
    import time

    for i in range(attempts):
        try:
            return fn()
        except TransientError as exc:
            if i == attempts - 1:
                log.warning("Википедия недоступна после %s попыток: %s", attempts, exc)
                raise
            time.sleep(wait * (i + 1))
    return None


def fetch_for_entity(name: str, url_es: str | None = None,
                     context: str = "") -> dict[str, Any] | None:
    """Статья по известному URL либо найденная поиском.

    Возвращает ещё и то, КАК она найдена. Это не деталь: у испанских имён
    сплошные совпадения, и поиск по имени легко приводит к однофамильцу из
    другой страны. Статья, найденная поиском, требует подтверждения человеком,
    статья по явному URL — нет.
    """
    if url_es and "/wiki/" in url_es:
        # Ссылка из браузера уже процентно закодирована, а summary() кодирует
        # заголовок сам. Без unquote «%C3%93scar» превращается в
        # «%25C3%2593scar», и Википедия отвечает 403 — то есть ровно на
        # именах с диакритикой, ради которых карточки и нужны.
        title = unquote(url_es.rsplit("/wiki/", 1)[1])
        lang = url_es.split("//", 1)[-1].split(".", 1)[0]
        got = _with_retry(lambda: summary(title, lang))
        if got:
            got["resolved_by"] = "url"
            got["candidates"] = []
            return got

    # Контекст сужает поиск: «Galán» без него находит однофамильцев. Но он же
    # его и топит: склеенные заголовки давали запрос в три сотни символов,
    # и поиск отвечал 500. Берём короткий хвост — уточнение, а не пересказ.
    hint = " ".join((context or "").split())[:_MAX_HINT]
    hits = search(f"{name} {hint}".strip()) or search(name)
    if not hits:
        return None
    got = summary(hits[0]["title"])
    if got is None:
        return None
    got["resolved_by"] = "search"
    got["candidates"] = hits[:5]
    return got
