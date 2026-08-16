"""Списки слов, которыми проверяются формулировки. Файлы — в config/.

Списками, а не регулярками в коде: их правят по итогам аудита, а править
конфиг безопаснее, чем код. Сравнение идёт по началу слова на
нормализованном тексте, поэтому в файлах лежат основы без окончаний.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from .config import CONFIG_DIR

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


@lru_cache(maxsize=8)
def load(name: str) -> tuple[str, ...]:
    """Строки файла без комментариев и пустых, нормализованные."""
    path = Path(CONFIG_DIR) / name
    if not path.exists():
        return ()
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        word = normalize(line)
        if word:
            out.append(word)
    return tuple(out)


def normalize(text: str) -> str:
    """Единая нормализация для всех проверок: регистр, диакритика, пунктуация.

    Своя, а не `textutil.normalize_title`: та ещё отрезает хвост издания
    («… — ABC.es»), и на факте «Председатель Grupo ACS — одной из крупнейших
    компаний» она съела бы половину строки вместе со смыслом.

    Диакритика снимается и с испанского, и с русского: «Sánchez» и «Sanchez»,
    «всё» и «все» должны сравниваться одинаково.
    """
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    text = "".join(
        ch for ch in unicodedata.normalize("NFD", text)
        if unicodedata.category(ch) != "Mn"
    )
    return _WS.sub(" ", _PUNCT.sub(" ", text)).strip()


def words(text: str) -> list[str]:
    return normalize(text).split()


def hits(text: str, words: tuple[str, ...]) -> list[str]:
    """Какие основы из списка встретились. Совпадение — по началу слова."""
    norm = normalize(text)
    if not norm:
        return []
    return [w for w in words if re.search(rf"\b{re.escape(w)}", norm)]
