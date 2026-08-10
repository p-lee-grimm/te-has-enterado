"""HTTP-слой: честный User-Agent, ретраи, условные запросы, уважение robots.txt."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .config import get_settings

log = logging.getLogger(__name__)

_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


@dataclass(slots=True)
class Fetched:
    url: str
    status: int
    text: str
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300

    @property
    def not_modified(self) -> bool:
        return self.status == 304


def user_agent() -> str:
    s = get_settings()
    ua = s.get_path("http.user_agent", "QuePasaBot/0.1")
    # если CONTACT_EMAIL не задан, подстановка оставила пустое место — не врём в UA
    return ua.replace("contact: )", "contact: unset)")


def make_client(**kwargs) -> httpx.AsyncClient:
    s = get_settings()
    return httpx.AsyncClient(
        headers={"User-Agent": user_agent(), "Accept-Language": "es-ES,es;q=0.9"},
        timeout=httpx.Timeout(float(s.get_path("http.timeout_seconds", 15))),
        follow_redirects=True,
        **kwargs,
    )


async def fetch(
    client: httpx.AsyncClient,
    url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    retries: int | None = None,
) -> Fetched:
    """GET с ретраями и условными заголовками. Никогда не бросает — возвращает Fetched с error."""
    s = get_settings()
    attempts = (retries if retries is not None else int(s.get_path("http.retries", 2))) + 1

    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    last_error = "unknown"
    for attempt in range(attempts):
        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code in _RETRYABLE_STATUS and attempt < attempts - 1:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            return Fetched(
                url=str(resp.url),
                status=resp.status_code,
                text="" if resp.status_code == 304 else resp.text,
                etag=resp.headers.get("etag"),
                last_modified=resp.headers.get("last-modified"),
                elapsed_ms=int(resp.elapsed.total_seconds() * 1000),
            )
        except Exception as exc:  # noqa: BLE001 — один битый источник не роняет прогон
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < attempts - 1:
                await asyncio.sleep(1.5 * (attempt + 1))

    return Fetched(url=url, status=0, text="", error=last_error)


async def head_status(client: httpx.AsyncClient, url: str) -> int:
    """Код ответа для проверки ссылок в gate. 0 = сетевая ошибка."""
    try:
        resp = await client.head(url)
        if resp.status_code in (405, 403, 501):  # HEAD не поддержан — пробуем GET
            resp = await client.get(url)
        return resp.status_code
    except Exception:  # noqa: BLE001
        return 0


class Limiter:
    """Общий потолок параллелизма плюс вежливость к каждому хосту.

    Без per-host ограничения крупное издание со 130 статьями в фиде получает
    130 запросов подряд и начинает отдавать ошибки — так теряется почти весь
    полный текст самого важного источника.
    """

    def __init__(self, total: int | None = None, per_host: int | None = None) -> None:
        s = get_settings()
        self._total = asyncio.Semaphore(total or int(s.get_path("http.max_concurrency", 8)))
        self._per_host_limit = per_host or int(s.get_path("http.max_concurrency_per_host", 3))
        self._delay = float(s.get_path("http.per_host_delay_seconds", 0.0))
        self._hosts: dict[str, asyncio.Semaphore] = {}

    def _host_sem(self, url: str) -> asyncio.Semaphore:
        host = urlsplit(url).netloc
        if host not in self._hosts:
            self._hosts[host] = asyncio.Semaphore(self._per_host_limit)
        return self._hosts[host]

    class _Slot:
        def __init__(self, limiter: "Limiter", url: str) -> None:
            self._l, self._url = limiter, url

        async def __aenter__(self):
            await self._l._total.acquire()
            self._host = self._l._host_sem(self._url)
            await self._host.acquire()
            return self

        async def __aexit__(self, *exc):
            if self._l._delay:
                await asyncio.sleep(self._l._delay)
            self._host.release()
            self._l._total.release()
            return False

    def slot(self, url: str) -> "Limiter._Slot":
        return Limiter._Slot(self, url)


class RobotsCache:
    """Кэш robots.txt на процесс. Требование §5.6."""

    def __init__(self) -> None:
        self._cache: dict[str, RobotFileParser | None] = {}
        self._lock = asyncio.Lock()

    async def allowed(self, client: httpx.AsyncClient, url: str) -> bool:
        if not get_settings().get_path("http.respect_robots_txt", True):
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"

        async with self._lock:
            if origin not in self._cache:
                self._cache[origin] = await self._load(client, origin)

        parser = self._cache[origin]
        if parser is None:  # robots.txt недоступен — трактуем как разрешение
            return True
        return parser.can_fetch(user_agent(), url)

    async def _load(self, client: httpx.AsyncClient, origin: str) -> RobotFileParser | None:
        res = await fetch(client, f"{origin}/robots.txt", retries=0)
        if not res.ok or not res.text.strip():
            return None
        parser = RobotFileParser()
        parser.parse(res.text.splitlines())
        return parser
