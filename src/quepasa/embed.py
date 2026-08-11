"""Эмбеддинги. Провайдер задаётся конфигом (embed.provider).

Испанский текст НЕ переводится перед эмбеддингом — модель обязана быть
мультиязычной (§1).
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import time

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

_RETRYABLE = {429, 500, 502, 503, 504}


def _post_with_retry(url: str, headers: dict, payload: dict) -> httpx.Response:
    """POST с экспоненциальной паузой на 429 и 5xx.

    У провайдеров эмбеддингов лимит запросов в минуту, и на новом ключе он может
    быть совсем низким. Пачка запросов подряд упирается в 429, и без ретраев
    стадия падает целиком, потеряв уже посчитанное.
    """
    s = get_settings()
    attempts = int(s.get_path("embed.retries", 5)) + 1
    backoff = float(s.get_path("embed.retry_backoff_seconds", 20))

    last: httpx.Response | None = None
    for attempt in range(attempts):
        resp = httpx.post(url, headers=headers, json=payload, timeout=180)
        if resp.status_code not in _RETRYABLE:
            return resp
        last = resp

        # провайдер может сам сказать, сколько ждать
        retry_after = resp.headers.get("retry-after")
        try:
            wait = float(retry_after) if retry_after else backoff * (2 ** attempt)
        except ValueError:
            wait = backoff * (2 ** attempt)

        if attempt < attempts - 1:
            log.warning(
                "Эмбеддинги: HTTP %s, ждём %.0f с (попытка %s из %s)",
                resp.status_code, wait, attempt + 1, attempts,
            )
            time.sleep(wait)

    return last  # type: ignore[return-value]


def estimate_tokens(text: str) -> int:
    """Грубая оценка числа токенов. Намеренно завышает.

    Точное число известно только после запроса, а решение о размере пачки надо
    принимать до него. Ошибка в большую сторону стоит лишнего запроса, ошибка
    в меньшую — отказа по лимиту, поэтому округляем вверх.
    """
    cpt = float(get_settings().get_path("embed.chars_per_token", 4))
    return max(1, int(len(text) / cpt) + 1)


class RateBudget:
    """Скользящее окно на минуту: следит и за запросами, и за токенами.

    У Voyage без привязанной карты лимит двойной — 3 запроса и 10 000 токенов
    в минуту. Упереться можно в любой из них, поэтому считаем оба.
    """

    def __init__(self) -> None:
        s = get_settings()
        self.rpm = float(s.get_path("embed.requests_per_minute", 0) or 0)
        self.tpm = float(s.get_path("embed.tokens_per_minute", 0) or 0)
        self._events: list[tuple[float, int]] = []  # (когда, сколько токенов)

    def _prune(self, now: float) -> None:
        self._events = [(t, n) for t, n in self._events if now - t < 60.0]

    def acquire(self, tokens: int) -> None:
        if self.rpm <= 0 and self.tpm <= 0:
            return
        while True:
            now = time.monotonic()
            self._prune(now)
            n_req = len(self._events)
            n_tok = sum(n for _, n in self._events)

            over_req = self.rpm > 0 and n_req + 1 > self.rpm
            over_tok = self.tpm > 0 and n_tok + tokens > self.tpm
            if not (over_req or over_tok):
                self._events.append((now, tokens))
                return

            # ждём, пока из окна выпадет самое старое событие
            oldest = min(t for t, _ in self._events)
            wait = max(0.5, 60.0 - (now - oldest) + 0.5)
            log.info(
                "Лимит провайдера (%s запр., %s ток. за минуту) — ждём %.0f с",
                n_req, n_tok, wait,
            )
            time.sleep(wait)


_budget: RateBudget | None = None


def _throttle(tokens: int = 0) -> None:
    global _budget
    if _budget is None:
        _budget = RateBudget()
    _budget.acquire(tokens)


def plan_batches(texts: list[str]) -> list[list[int]]:
    """Режет корпус на пачки так, чтобы каждая влезала в лимит токенов.

    Возвращает списки индексов, а не сами тексты: вызывающему нужно сопоставить
    результат со статьями.
    """
    s = get_settings()
    max_items = int(s.require("embed.batch_size"))
    max_tokens = int(s.get_path("embed.max_tokens_per_request", 0) or 0)

    batches: list[list[int]] = []
    cur: list[int] = []
    cur_tokens = 0

    for i, text in enumerate(texts):
        n = estimate_tokens(text)
        too_many = len(cur) >= max_items
        too_big = max_tokens and cur and cur_tokens + n > max_tokens
        if too_many or too_big:
            batches.append(cur)
            cur, cur_tokens = [], 0
        cur.append(i)
        cur_tokens += n

    if cur:
        batches.append(cur)
    return batches


def _voyage(texts: list[str], model: str, dim: int) -> list[list[float]]:
    key = env("VOYAGE_API_KEY", required=True)
    _throttle(sum(estimate_tokens(t) for t in texts))
    resp = _post_with_retry(
        "https://api.voyageai.com/v1/embeddings",
        {"Authorization": f"Bearer {key}"},
        {"input": texts, "model": model, "input_type": "document",
         "output_dimension": dim},
    )
    if resp.status_code == 429:
        raise EmbeddingError(
            "Voyage отвечает 429 даже после ретраев. Проверь embed.tokens_per_minute "
            "и embed.requests_per_minute в config/settings.yaml — на бесплатном "
            "тарифе это 10000 и 3. Привязка карты в биллинге Voyage поднимает "
            "лимиты на порядки."
        )
    resp.raise_for_status()
    data = sorted(resp.json()["data"], key=lambda d: d["index"])
    return [d["embedding"] for d in data]


def _openai(texts: list[str], model: str, dim: int) -> list[list[float]]:
    key = env("OPENAI_API_KEY", required=True)
    _throttle(sum(estimate_tokens(t) for t in texts))
    resp = _post_with_retry(
        "https://api.openai.com/v1/embeddings",
        {"Authorization": f"Bearer {key}"},
        {"input": texts, "model": model, "dimensions": dim},
    )
    resp.raise_for_status()
    data = sorted(resp.json()["data"], key=lambda d: d["index"])
    return [d["embedding"] for d in data]


def _cohere(texts: list[str], model: str, dim: int) -> list[list[float]]:
    key = env("COHERE_API_KEY", required=True)
    _throttle(sum(estimate_tokens(t) for t in texts))
    resp = _post_with_retry(
        "https://api.cohere.com/v2/embed",
        {"Authorization": f"Bearer {key}"},
        {"texts": texts, "model": model, "input_type": "search_document",
         "embedding_types": ["float"]},
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
