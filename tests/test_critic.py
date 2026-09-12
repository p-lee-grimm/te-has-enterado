"""Критик поста: решение о судьбе текста, без вызовов модели.

Проверяется то, что остаётся нашим: как разбирается ответ, что происходит
при сбое и какая подсказка уходит на переписывание.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quepasa.critic import clean, hint_for_retry, user_message  # noqa: E402
from quepasa.posts import is_thin  # noqa: E402

NOW = datetime.now(timezone.utc)


def test_unclear_answer_lets_the_post_through():
    """Критик — страховка, а не ворота: сломался он — выходит пост, а не
    останавливается канал."""
    assert clean({})["conveys"] is True
    assert clean({"conveys": "нет"})["conveys"] is True
    assert clean({"conveys": None})["conveys"] is True


def test_explicit_refusal_is_kept():
    v = clean({"conveys": False, "missing": "что именно сказано",
               "fix": "критику нельзя путать с дискредитацией"})
    assert v["conveys"] is False
    assert v["missing"] == "что именно сказано"


def test_hint_names_the_fact_to_put_in_the_headline():
    hint = hint_for_retry({"conveys": False, "missing": "суть заявления",
                           "fix": "запрет вступает в силу с января"})
    assert "запрет вступает в силу с января" in hint
    assert "суть заявления" in hint


def test_user_message_shows_sources_and_post():
    msg = user_message(["La fiscal general advierte"], "**Заголовок**\n\nЛид")
    assert "La fiscal general advierte" in msg
    # звёздочки markdown критику только мешают
    assert "**" not in msg
    assert "Лид" in msg


def test_thin_mark_expires():
    """Сюжет развивается: вчерашняя пустота сегодня может стать новостью."""
    assert is_thin({"thin_at": NOW}) is True
    assert is_thin({"thin_at": NOW - timedelta(days=2)}) is False
    assert is_thin({"thin_at": None}) is False
