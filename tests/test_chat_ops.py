"""Работа из чата: команды и правка вышедшего поста ответом.

Консоль владельцу недоступна — он работает с телефона. И согласовывать
каждый пост он не может: их два десятка в сутки, очередь на подтверждение
останавливает канал целиком. Поэтому пост выходит сам, а владелец получает
уведомление постфактум и правит текст ответом.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from quepasa.commands import COMMANDS, run_command  # noqa: E402
from quepasa.lint import rare_words  # noqa: E402
from quepasa.posts import published_message_id_in  # noqa: E402


class TestCommands:
    def test_help_lists_every_command(self):
        out = run_command("/help")
        for name in COMMANDS:
            if name != "/start":
                assert name in out

    def test_unknown_command_answers_with_help(self):
        """Молчание владелец не отличает от поломки."""
        out = run_command("/чтотоне")
        assert "Не знаю" in out and "/status" in out

    def test_command_with_bot_suffix(self):
        """В группе Telegram дописывает @имя_бота."""
        assert "Команды" in run_command("/help@tehasenterado_bot")

    def test_failure_is_reported_not_swallowed(self, monkeypatch):
        import quepasa.commands as cmd

        def boom():
            raise RuntimeError("база недоступна")

        monkeypatch.setitem(cmd.COMMANDS, "/status", boom)
        out = run_command("/status")
        assert "не выполнилась" in out and "база недоступна" in out


class TestPublishedReply:
    """Ответ на уведомление о посте — это новая шапка."""

    def test_message_id_parsed(self):
        text = "📣 Пост 118 вышел\n\nЗаголовок поста"
        assert published_message_id_in(text) == 118

    def test_other_notifications_are_not_posts(self):
        assert published_message_id_in("Факт 12 снят") is None
        assert published_message_id_in("Новые сущности в очереди") is None

    def test_empty_reply_changes_nothing(self):
        from quepasa.posts import rewrite_published
        assert "Пустой" in rewrite_published(118, "   ")


class TestRareWords:
    """Проверять частые слова незачем: выдумка не повторяется из поста в пост."""

    ROWS = [
        {"message_id": 1, "header_md": "**Наводнение в Леоне**\n\nРиада затопила"},
        {"message_id": 2, "header_md": "**Наводнение в Валенсии**\n\nвода прибывает"},
        {"message_id": 3, "header_md": "**Наводнение снова**\n\nвода прибывает"},
    ]

    def test_rare_word_kept_with_its_post(self):
        out = rare_words(self.ROWS)
        assert out.get("риада") == 1

    def test_frequent_word_dropped(self):
        out = rare_words(self.ROWS)
        assert "наводнение" not in out, "встречается трижды — не выдумка"

    def test_short_words_ignored(self):
        assert "вода" not in rare_words(self.ROWS)

    @pytest.mark.parametrize("word", ["абучеали", "деррибировали", "ремонтаду"])
    def test_real_cases_would_surface(self, word):
        rows = [{"message_id": 9, "header_md": f"**Заголовок**\n\nтекст {word} текст"}]
        assert word in rare_words(rows)
