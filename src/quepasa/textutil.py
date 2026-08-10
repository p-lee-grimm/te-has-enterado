"""Нормализация URL и текста. Всё, что покрыто тестами (§8.6)."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Трекинговые параметры, которые не влияют на содержимое страницы.
_TRACKING_PREFIXES = ("utm_", "pk_", "at_", "ns_", "_ga", "mtm_", "piwik_")
_TRACKING_EXACT = {
    "fbclid", "gclid", "gbraid", "wbraid", "msclkid", "yclid", "igshid",
    "ref", "referer", "referrer", "source", "cmp", "ncid", "sid",
    "s_kwcid", "spm", "share", "sharedfrom", "cid", "outputtype",
}

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
# Хвост «| El País», «- ABC.es» и подобное в заголовках из фидов.
_TITLE_TAIL_RE = re.compile(r"\s*[|–—-]\s*[^|–—-]{2,30}$")


def canonical_url(url: str) -> str:
    """Канонический URL: без utm и фрагмента, нормализованные схема и хост.

    Дедуп в §3.3 держится на этой функции, поэтому она обязана быть стабильной:
    один и тот же материал из разных перезаливов фида должен давать одну строку.
    """
    url = (url or "").strip()
    if not url:
        return ""

    parts = urlsplit(url)
    scheme = (parts.scheme or "https").lower()
    if scheme not in ("http", "https"):
        return url

    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    # amp-зеркала ведут на тот же материал
    if host.startswith("amp."):
        host = host[4:]

    netloc = host
    if parts.port and parts.port not in (80, 443):
        netloc = f"{host}:{parts.port}"

    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not (
            k.lower() in _TRACKING_EXACT
            or k.lower().startswith(_TRACKING_PREFIXES)
        )
    ]
    query.sort()

    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path.endswith("/amp"):
        path = path[: -len("/amp")]
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    return urlunsplit((scheme, netloc, path or "/", urlencode(query), ""))


def normalize_title(title: str) -> str:
    """Заголовок к сравнимому виду: без регистра, диакритики, пунктуации и хвоста издания."""
    text = unicodedata.normalize("NFKC", title or "").strip()
    text = _TITLE_TAIL_RE.sub("", text)
    text = text.casefold()
    text = "".join(
        ch for ch in unicodedata.normalize("NFD", text)
        if unicodedata.category(ch) != "Mn"
    )
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def title_hash(title: str) -> str:
    return hashlib.sha256(normalize_title(title).encode("utf-8")).hexdigest()[:32]


def normalize_words(text: str) -> list[str]:
    """Слова для проверки на скрытое цитирование (§3.10)."""
    return normalize_title(text).split()


def shingles(words: list[str], size: int) -> set[str]:
    if len(words) < size:
        return set()
    return {" ".join(words[i : i + size]) for i in range(len(words) - size + 1)}


def longest_common_shingle(candidate: str, source_text: str, size: int) -> str | None:
    """Возвращает первое совпадающее окно из `size` слов или None.

    Механическая проверка: пересказ не должен содержать дословных кусков статьи.
    """
    cand_words = normalize_words(candidate)
    cand = shingles(cand_words, size)
    if not cand:
        return None
    src = shingles(normalize_words(source_text), size)
    common = cand & src
    return next(iter(sorted(common))) if common else None


def count_sentences(text: str) -> int:
    """Грубый счёт предложений — для проверки лимита в 3–4 (§5.3)."""
    stripped = (text or "").strip()
    if not stripped:
        return 0
    return len([s for s in re.split(r"[.!?…]+(?:\s|$)", stripped) if s.strip()])


def strip_html(html: str) -> str:
    """Снимает разметку с анонса из фида — там часто приходит HTML."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (
        text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
        .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    )
    return _WS_RE.sub(" ", text).strip()
