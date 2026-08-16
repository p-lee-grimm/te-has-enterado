"""Проверки текста, которые нельзя сделать кодом.

Выдуманное слово от настоящего отличает словарь русского языка, а его
у нас нет. Поэтому спрашиваем модель — но не построчно, а одним вызовом
на весь список редких слов: так это стоит копейки и годится для регулярной
проверки.

Класс ошибок реальный: за неделю в канал ушли «абучеали» (abuchear),
«деррибировали» (derribar), «ремонтаду» (remontada) и «риада» (riada).
Правило «переводи, а не транскрибируй» в промпте было и тогда.
"""

from __future__ import annotations

import collections
import logging
import re

log = logging.getLogger(__name__)

# Короткие слова не берём: там слишком много служебных, а транскрипции
# испанских глаголов всегда длиннее.
_WORD = re.compile(r"[а-яёА-ЯЁ]{5,}")

SYSTEM = (
    "Тебе дают список слов из русских новостных заголовков об Испании. "
    "Верни строго JSON {\"bad\": [\"слово\", ...]} — только те слова, которых "
    "НЕТ в русском языке: выдуманные транскрипции испанских глаголов и "
    "нарицательных, например «абучеать» от abuchear или «ремонтада» от "
    "remontada. Имена собственные, топонимы, фамилии и нормальные русские "
    "слова в список НЕ включай. Если всё в порядке — верни пустой список."
)


def rare_words(rows: list[dict], max_count: int = 2) -> dict[str, int]:
    """Слова, встретившиеся не чаще max_count раз, и где встретились.

    Частые слова проверять незачем: выдумка не повторяется из поста в пост.
    """
    seen: collections.Counter[str] = collections.Counter()
    where: dict[str, int] = {}
    for r in rows:
        for w in _WORD.findall(r.get("header_md") or ""):
            lw = w.lower()
            seen[lw] += 1
            where.setdefault(lw, r.get("message_id"))
    return {w: where[w] for w, n in seen.items() if n <= max_count}


def invented_words() -> list[tuple[str, int]]:
    """Ищет в вышедших постах слова, которых нет в русском языке."""
    from .config import get_settings
    from .db import connect
    from .llm import _PROVIDERS, LLMUsage, extract_json

    with connect() as conn:
        rows = conn.execute(
            "SELECT message_id, header_md FROM posts WHERE status = 'published'"
        ).fetchall()

    candidates = rare_words([dict(r) for r in rows])
    if not candidates:
        return []

    fn = _PROVIDERS[get_settings().require("summarize.provider")]
    try:
        data = extract_json(fn(SYSTEM, "Слова:\n" + ", ".join(sorted(candidates)),
                               LLMUsage()))
    except Exception as exc:  # noqa: BLE001 — проверка не должна ронять команду
        log.warning("Проверка слов не выполнилась: %s", exc)
        return []

    out = []
    for w in data.get("bad") or []:
        mid = candidates.get(str(w).lower())
        if mid:
            out.append((str(w), mid))
    return out
