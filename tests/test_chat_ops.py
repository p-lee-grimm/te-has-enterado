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


class TestSilenceWatchdog:
    """Трёхдневный простой не должен пройти незамеченным.

    25 августа --check-facts повис на сетевом чтении и держал лок publish.
    Автопостинг не запускался трое суток: каждый следующий прогон видел
    лок и уходил. Узнать об этом было неоткуда — статус смотрят руками.
    """

    @staticmethod
    def _run(monkeypatch, hour, age_h, alerted=None):
        import datetime as dt

        import quepasa.status as st
        import quepasa.telegram as tg
        sent = []
        monkeypatch.setattr(tg, "notify_owner", lambda t, **k: sent.append(t))

        class Conn:
            def execute(self, sql, params=None):
                if "max(published_at)" in sql:
                    return type("R", (), {"fetchone": lambda s: {"h": age_h}})()
                if "bot_state" in sql and sql.strip().startswith("SELECT"):
                    row = {"value": alerted} if alerted else None
                    return type("R", (), {"fetchone": lambda s: row})()
                return type("R", (), {"fetchone": lambda s: None})()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        import quepasa.db as db
        monkeypatch.setattr(db, "connect", lambda *a, **k: Conn())

        from zoneinfo import ZoneInfo
        now = dt.datetime(2026, 8, 28, hour, 0, tzinfo=ZoneInfo("Europe/Madrid"))
        st.watch_silence(now=now)
        return sent

    def test_alerts_inside_window(self, monkeypatch):
        assert self._run(monkeypatch, hour=14, age_h=72.0)

    def test_silent_outside_window(self, monkeypatch):
        """Ночью тишина штатная — будить из-за неё нельзя."""
        assert not self._run(monkeypatch, hour=3, age_h=72.0)

    def test_silent_when_posts_are_fresh(self, monkeypatch):
        assert not self._run(monkeypatch, hour=14, age_h=1.0)

    def test_not_repeated_within_a_day(self, monkeypatch):
        """Сторож, пишущий каждые пять минут, перестают читать."""
        recent = "2026-08-28T10:00:00+02:00"
        assert not self._run(monkeypatch, hour=14, age_h=72.0, alerted=recent)


class TestPublicationCheck:
    """Включённого переключателя мало: он был включён все три дня простоя."""

    @staticmethod
    def _check(age_h, monkeypatch):
        import quepasa.posts as posts
        # переключатель здесь ни при чём: все три дня простоя он был включён
        monkeypatch.setattr(posts, "autopost_enabled", lambda: True)
        from quepasa.status import checks
        data = {
            "fetch_age_h": 0.1, "post_age_h": age_h,
            "articles": {"last_fetch": None, "last_day": 900,
                         "no_embedding": 0, "no_cluster": 0},
            "feeds": {"silent": 0, "active": 17},
            "posts": {"last_post": None},
            "clusters": {"big": 20},
            "queue": {"unresolved": 0, "drafts": 0, "edits": 0},
        }
        for name, ok, detail in checks(data):
            if name == "публикация":
                return ok, detail
        raise AssertionError("проверки публикации нет")

    def test_stale_publication_fails(self, monkeypatch):
        ok, detail = self._check(78.9, monkeypatch)
        assert not ok, "трое суток тишины — это не «в порядке»"
        assert "порог" in detail

    def test_overnight_gap_is_fine(self, monkeypatch):
        """Окно закрыто с 21 до 9 — утром свежесть в 12 часов нормальна."""
        ok, _ = self._check(12.0, monkeypatch)
        assert ok


class TestFixCommand:
    """Удалить пост владелец может сам в канале; отредактировать — нет.

    Сообщение бота в канале правит только сам бот, поэтому из всего,
    что делало уведомление «📣 Пост вышел», осталась одна нужная часть —
    и та по запросу, а не двадцать раз в сутки.
    """

    def test_help_without_arguments(self):
        out = run_command("/fix")
        assert "/fix" in out and "445" in out

    def test_help_when_number_missing(self):
        assert "заменить текст" in run_command("/fix просто текст")

    def test_help_when_text_missing(self):
        assert "заменить текст" in run_command("/fix 445")

    def test_calls_rewrite_with_number_and_text(self, monkeypatch):
        import quepasa.posts as posts
        seen = {}
        monkeypatch.setattr(posts, "rewrite_published",
                            lambda mid, text: seen.update(mid=mid, text=text) or "ок")
        assert run_command("/fix 445 Новый заголовок\nи лид") == "ок"
        assert seen["mid"] == 445
        assert seen["text"] == "Новый заголовок\nи лид"

    def test_other_commands_ignore_arguments(self):
        """Аргумент есть только у /fix; остальные его не ждут."""
        assert "Команды" in run_command("/help лишнее слово")
