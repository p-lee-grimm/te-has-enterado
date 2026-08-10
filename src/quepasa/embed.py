"""Эмбеддинги. Провайдер задаётся конфигом (embed.provider).

Испанский текст НЕ переводится перед эмбеддингом — модель обязана быть
мультиязычной (§1).
"""

from __future__ import annotations

import hashlib
import logging
import math
import re

import httpx

from .config import env, get_settings

log = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    pass


def build_input(title: str, text: str) -> str:
    """§3.4 — эмбеддинг считается по title + первые N символов текста."""
    n = int(get_settings().require("embed.body_chars"))
    return f"{title}\n{(text or '')[:n]}".strip()


# ------------------------------------------------------------------ провайдеры

def _voyage(texts: list[str], model: str, dim: int) -> list[list[float]]:
    key = env("VOYAGE_API_KEY", required=True)
    resp = httpx.post(
        "https://api.voyageai.com/v1/embeddings",
        headers={"Authorization": f"Bearer {key}"},
        json={"input": texts, "model": model, "input_type": "document",
              "output_dimension": dim},
        timeout=120,
    )
    resp.raise_for_status()
    data = sorted(resp.json()["data"], key=lambda d: d["index"])
    return [d["embedding"] for d in data]


def _openai(texts: list[str], model: str, dim: int) -> list[list[float]]:
    key = env("OPENAI_API_KEY", required=True)
    resp = httpx.post(
        "https://api.openai.com/v1/embeddings",
        headers={"Authorization": f"Bearer {key}"},
        json={"input": texts, "model": model, "dimensions": dim},
        timeout=120,
    )
    resp.raise_for_status()
    data = sorted(resp.json()["data"], key=lambda d: d["index"])
    return [d["embedding"] for d in data]


def _cohere(texts: list[str], model: str, dim: int) -> list[list[float]]:
    key = env("COHERE_API_KEY", required=True)
    resp = httpx.post(
        "https://api.cohere.com/v2/embed",
        headers={"Authorization": f"Bearer {key}"},
        json={"texts": texts, "model": model, "input_type": "search_document",
              "embedding_types": ["float"]},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["embeddings"]["float"]


_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _local(texts: list[str], model: str, dim: int) -> list[list[float]]:
    """Детерминированный офлайн-эмбеддер. ТОЛЬКО для разработки и тестов.

    Это мешок слов, разложенный по dim измерениям хэшем, с idf-подобным весом
    по длине слова. Ловит лексическое сходство (одни и те же имена и топонимы
    в сообщениях об одном событии), но не синонимию и не перефраз.

    Годится, чтобы прогнать пайплайн без ключей и посмотреть на механику
    кластеризации. НЕ годится для калибровки порога: значения косинуса тут
    живут в другом диапазоне, чем у настоящей мультиязычной модели.
    """
    out: list[list[float]] = []
    for text in texts:
        vec = [0.0] * dim
        tokens = _TOKEN_RE.findall(text.casefold())
        for tok in tokens:
            if len(tok) < 3:
                continue
            h = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(h[:4], "big") % dim
            sign = 1.0 if h[4] & 1 else -1.0
            vec[idx] += sign * (1.0 + math.log(len(tok)))
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        out.append([v / norm for v in vec])
    return out


_PROVIDERS = {
    "voyage": _voyage,
    "openai": _openai,
    "cohere": _cohere,
    "local": _local,
}


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    s = get_settings()
    provider = s.require("embed.provider")
    model = s.require("embed.model")
    dim = int(s.require("embed.dimensions"))

    fn = _PROVIDERS.get(provider)
    if fn is None:
        raise EmbeddingError(
            f"Неизвестный провайдер эмбеддингов: {provider}. "
            f"Доступны: {', '.join(_PROVIDERS)}"
        )
    if provider == "local":
        log.warning(
            "embed.provider=local — офлайн-заглушка, не для калибровки и не для прода"
        )

    vecs = fn(texts, model, dim)
    for v in vecs:
        if len(v) != dim:
            raise EmbeddingError(
                f"{provider}/{model} вернул размерность {len(v)}, "
                f"а в settings.yaml embed.dimensions={dim}"
            )
    return vecs


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
