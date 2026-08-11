"""Гео-теги: закрытый словарь и матчинг (§4.3).

Тег ставится, только если сужает. Национальная новость тега не получает:
#Испания в канале про Испанию не сужает ничего.
"""

from __future__ import annotations

import logging
import unicodedata
from functools import lru_cache

import yaml

from .config import CONFIG_DIR

log = logging.getLogger(__name__)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFD", (text or "").casefold())
    return "".join(c for c in text if unicodedata.category(c) != "Mn").strip()


@lru_cache(maxsize=1)
def _load() -> tuple[dict[str, str], set[str]]:
    path = CONFIG_DIR / "geo_tags.yaml"
    if not path.exists():
        return {}, set()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    alias_to_tag: dict[str, str] = {}
    tags: set[str] = set()
    for item in raw.get("tags", []):
        tag = item["tag"]
        tags.add(tag)
        alias_to_tag[_norm(tag.lstrip("#"))] = tag
        for alias in item.get("aliases", []):
            alias_to_tag[_norm(alias)] = tag
    return alias_to_tag, tags


def known_tags() -> set[str]:
    return _load()[1]


def resolve(value: str | None) -> str | None:
    """Приводит значение от модели к тегу из словаря либо отбрасывает.

    Отброшенное пишется в лог: по этим записям владелец решает, добавить
    алиас или это мусор.
    """
    if not value:
        return None
    alias_to_tag, tags = _load()
    raw = value.strip()
    if raw in tags:
        return raw
    hit = alias_to_tag.get(_norm(raw.lstrip("#")))
    if hit:
        return hit
    log.warning("Гео-тег вне словаря, отброшен: %r", raw)
    return None
