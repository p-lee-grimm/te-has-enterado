"""Поведение при лимитах провайдера эмбеддингов.

429 не должен ронять стадию: на новом ключе лимит низкий, а корпус большой.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402


def resp(status, headers=None, payload=None):
    return SimpleNamespace(
        status_code=status,
        headers=headers or {},
        json=lambda: payload or {},
        raise_for_status=lambda: None,
    )


class TestRetry:
    def test_success_passes_through_without_waiting(self, monkeypatch):
        import quepasa.embed as em

        calls = []
        monkeypatch.setattr(em.httpx, "post", lambda *a, **k: (calls.append(1), resp(200))[1])
        monkeypatch.setattr(em.time, "sleep", lambda s: pytest.fail("ждать не должны"))
        assert em._post_with_retry("u", {}, {}).status_code == 200
        assert len(calls) == 1

    def test_retries_on_429_then_succeeds(self, monkeypatch):
        import quepasa.embed as em

        seq = [resp(429), resp(429), resp(200)]
        waited = []
        monkeypatch.setattr(em.httpx, "post", lambda *a, **k: seq.pop(0))
        monkeypatch.setattr(em.time, "sleep", waited.append)

        assert em._post_with_retry("u", {}, {}).status_code == 200
        assert len(waited) == 2
        # пауза растёт, а не долбит с той же частотой
        assert waited[1] > waited[0]

    def test_respects_retry_after_header(self, monkeypatch):
        import quepasa.embed as em

        seq = [resp(429, {"retry-after": "7"}), resp(200)]
        waited = []
        monkeypatch.setattr(em.httpx, "post", lambda *a, **k: seq.pop(0))
        monkeypatch.setattr(em.time, "sleep", waited.append)

        em._post_with_retry("u", {}, {})
        assert waited == [7.0]

    def test_gives_up_and_returns_last(self, monkeypatch):
        import quepasa.embed as em

        monkeypatch.setattr(em.httpx, "post", lambda *a, **k: resp(429))
        monkeypatch.setattr(em.time, "sleep", lambda s: None)
        assert em._post_with_retry("u", {}, {}).status_code == 429

    def test_voyage_raises_readable_error_after_giving_up(self, monkeypatch):
        import quepasa.embed as em

        monkeypatch.setattr(em, "env", lambda *a, **k: "ключ")
        monkeypatch.setattr(em, "_throttle", lambda tokens=0: None)
        monkeypatch.setattr(em, "_post_with_retry", lambda *a, **k: resp(429))
        with pytest.raises(em.EmbeddingError, match="requests_per_minute"):
            em._voyage(["текст"], "voyage-3.5", 1024)

    def test_server_errors_also_retried(self, monkeypatch):
        import quepasa.embed as em

        seq = [resp(503), resp(200)]
        monkeypatch.setattr(em.httpx, "post", lambda *a, **k: seq.pop(0))
        monkeypatch.setattr(em.time, "sleep", lambda s: None)
        assert em._post_with_retry("u", {}, {}).status_code == 200

    def test_client_error_not_retried(self, monkeypatch):
        """401 повторять бессмысленно — ключ от этого не починится."""
        import quepasa.embed as em

        calls = []
        monkeypatch.setattr(em.httpx, "post",
                            lambda *a, **k: (calls.append(1), resp(401))[1])
        monkeypatch.setattr(em.time, "sleep", lambda s: pytest.fail("ждать не должны"))
        assert em._post_with_retry("u", {}, {}).status_code == 401
        assert len(calls) == 1


class TestRateBudget:
    """Двойной лимит: и по запросам, и по токенам."""

    def _budget(self, monkeypatch, rpm, tpm):
        import quepasa.embed as em

        monkeypatch.setattr(
            em, "get_settings",
            lambda: type("S", (), {
                "get_path": lambda self, k, d=None: {
                    "embed.requests_per_minute": rpm,
                    "embed.tokens_per_minute": tpm,
                }.get(k, d)
            })(),
        )
        return em.RateBudget()

    def test_no_limits_never_waits(self, monkeypatch):
        import quepasa.embed as em

        monkeypatch.setattr(em.time, "sleep", lambda s: pytest.fail("ждать не должны"))
        b = self._budget(monkeypatch, 0, 0)
        for _ in range(50):
            b.acquire(100_000)

    def test_request_limit_forces_wait(self, monkeypatch):
        import quepasa.embed as em

        waited = []
        monkeypatch.setattr(em.time, "sleep", lambda s: waited.append(s))
        b = self._budget(monkeypatch, 3, 0)
        for _ in range(3):
            b.acquire(10)
        assert not waited
        # четвёртый запрос в ту же минуту должен упереться
        b._events = b._events[:3]
        monkeypatch.setattr(em.time, "sleep", lambda s: b._events.clear())
        b.acquire(10)

    def test_token_limit_forces_wait(self, monkeypatch):
        import quepasa.embed as em

        b = self._budget(monkeypatch, 0, 10_000)
        b.acquire(9_000)
        # следующая пачка не влезает в токенный лимит
        monkeypatch.setattr(em.time, "sleep", lambda s: b._events.clear())
        b.acquire(5_000)
        assert sum(n for _, n in b._events) == 5_000
