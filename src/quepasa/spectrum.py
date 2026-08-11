"""Политическая шкала: значение, бакет, размах, скор.

Единственное место, где метка вроде `center-left` превращается в число.
Шкала и границы бакетов лежат в config/settings.yaml, в коде их нет (§13.4).

Два правила, без которых всё считается неверно:

  Агентства не имеют политической координаты. EFE и Europa Press пишут обо
  всём подряд; если считать их центром, правило «фланг + центр» вырождается
  в «два любых источника».

  Считаем владельцев, а не издания. Три газеты одного холдинга с одной ленты —
  это одно подтверждение, а не три.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

from .config import get_settings

LEFT, CENTER, RIGHT = "left", "center", "right"


def lean_value(lean: str) -> int | None:
    """Число по метке. None — метки нет в шкале (её надо завести руками)."""
    scale = get_settings().require("lean_scale")
    value = scale.get(lean)
    return int(value) if value is not None else None


def bucket(lean: str) -> str | None:
    value = lean_value(lean)
    if value is None:
        return None
    b = get_settings().require("lean_bucket")
    if value <= int(b["left_max"]):
        return LEFT
    if value >= int(b["right_min"]):
        return RIGHT
    return CENTER


def political_sources(sources: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Только те, у кого есть политическая координата: без агентств.

    Официальные источники (BOE, Moncloa) тоже не имеют координаты — это
    первоисточник, а не позиция, — поэтому в размах и ветки они не входят.
    """
    return [s for s in sources if s.get("type") not in ("agency", "official")]


def owner_of(source: dict[str, Any]) -> str:
    """Владелец источника; если не проставлен — сам источник."""
    return source.get("owner_group") or source.get("source_id") or source.get("id", "")


def n_owners(sources: Iterable[dict[str, Any]]) -> int:
    return len({owner_of(s) for s in sources})


def span(sources: Iterable[dict[str, Any]]) -> int:
    """Размах по шкале среди источников с политической координатой.

    0, если представлен один фланг или считать не по чему.
    """
    values = [
        v for v in (lean_value(s["lean"]) for s in political_sources(sources))
        if v is not None
    ]
    return max(values) - min(values) if len(values) >= 2 else 0


def buckets(sources: Iterable[dict[str, Any]]) -> set[str]:
    return {
        b for b in (bucket(s["lean"]) for s in political_sources(sources))
        if b is not None
    }


def score(sources: Iterable[dict[str, Any]], age_hours: float) -> float:
    """score = (n_owners + w_span × span) × exp(−Δt / half_life).

    Затухание обязательно: без него вчерашний широкий сюжет вечно обгоняет
    сегодняшний.
    """
    s = get_settings()
    w_span = float(s.require("rank.w_span"))
    half_life = float(s.require("rank.half_life_hours"))

    sources = list(sources)
    # владельцев считаем по всем источникам: агентство подтверждает, что
    # событие произошло, — оно не даёт координаты, но даёт факт
    raw = n_owners(sources) + w_span * span(sources)
    decay = math.exp(-math.log(2) * max(0.0, age_hours) / half_life) if half_life > 0 else 1.0
    return raw * decay


def passes_rule(sources: Iterable[dict[str, Any]]) -> bool:
    """Три ветки допуска, объединённые по ИЛИ (§2 спеки контекстного слоя)."""
    s = get_settings()
    rules = s.get_path("autopost.rules", {}) or {}
    min_owners = int(s.get_path("autopost.min_sources", 3))

    sources = list(sources)
    bs = buckets(sources)

    if rules.get("left_and_right", True) and LEFT in bs and RIGHT in bs:
        return True
    if rules.get("side_and_center", True) and (LEFT in bs or RIGHT in bs) and CENTER in bs:
        return True
    if rules.get("min_sources", True) and n_owners(political_sources(sources)) >= min_owners:
        return True
    return False


def is_one_sided(sources: Iterable[dict[str, Any]]) -> bool:
    """Все источники с координатой в одном бакете.

    Ветка «≥N владельцев» пропускает синхронные кампании одного лагеря. Это не
    повод запрещать, но читателю показывают, что подтверждения с другой стороны
    пока нет.
    """
    bs = buckets(sources)
    return len(bs) == 1
