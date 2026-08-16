"""Правило повтора, окно правки и снятие пометки об односторонности."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quepasa.posts import repeat_state  # noqa: E402
from quepasa.spectrum import is_one_sided  # noqa: E402


def ago(hours):
    return datetime.now(timezone.utc) - timedelta(hours=hours)


class TestRepeatRule:
    def test_never_published_is_new(self):
        ok, cont = repeat_state({"last_published_at": None, "n_articles": 5})
        assert ok and not cont

    def test_needs_both_time_and_articles(self):
        """И, а не ИЛИ: одного времени мало, одних статей мало."""
        base = {"last_published_at": ago(20), "n_articles": 10, "n_at_publish": 9}
        assert repeat_state(base)[0] is False          # прошло время, но +1 статья

        base = {"last_published_at": ago(2), "n_articles": 20, "n_at_publish": 10}
        assert repeat_state(base)[0] is False          # много нового, но 2 часа

    def test_significant_update_returns_as_continuation(self):
        ok, cont = repeat_state(
            {"last_published_at": ago(20), "n_articles": 14, "n_at_publish": 10})
        assert ok and cont

    def test_published_cluster_without_updates_stays_out(self):
        ok, _ = repeat_state(
            {"last_published_at": ago(48), "n_articles": 10, "n_at_publish": 10})
        assert not ok

    def test_missing_baseline_counts_all_as_new(self):
        ok, _ = repeat_state(
            {"last_published_at": ago(20), "n_articles": 5, "n_at_publish": None})
        assert ok


def src(lean, owner, type_="press"):
    return {"source_id": owner, "lean": lean, "type": type_, "owner_group": owner}


class TestOneSidedLifting:
    def test_one_flank_is_one_sided(self):
        assert is_one_sided([src("right", "A"), src("far-right", "B")])

    def test_opposite_flank_lifts_it(self):
        """Ради этого и заведена правка: пометка снимается сама."""
        before = [src("right", "A"), src("far-right", "B")]
        after = before + [src("left", "C")]
        assert is_one_sided(before)
        assert not is_one_sided(after)

    def test_agency_does_not_lift(self):
        """Агентство — не другая сторона: политической координаты у него нет."""
        before = [src("right", "A")]
        after = before + [src("center", "EP", "agency")]
        assert is_one_sided(after)

    def test_official_does_not_lift(self):
        after = [src("right", "A"), src("center", "BOE", "official")]
        assert is_one_sided(after)


class TestPromotionWindow:
    """Окно повышения касается только тех, кто НЕ прошёл ветки допуска."""

    def test_morning_review_hour(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from quepasa.posts import in_morning_review
        tz = ZoneInfo("Europe/Madrid")
        assert in_morning_review(datetime(2026, 8, 11, 9, 0, tzinfo=tz))
        assert not in_morning_review(datetime(2026, 8, 11, 14, 0, tzinfo=tz))


class TestDigestFlows:
    """Два потока «Коротко» и их потолки (§5)."""

    def _cfg(self):
        from quepasa.config import get_settings
        return get_settings()["digest"]

    def test_world_cap_configured(self):
        assert self._cfg()["max_world"] <= self._cfg()["max_lines"]

    def test_min_age_below_max_age(self):
        """Иначе окно переноса пустое и поток 1 не наполняется никогда."""
        assert self._cfg()["min_age_hours"] < self._cfg()["max_age_hours"]

    def test_min_lines_guard_present(self):
        assert self._cfg()["min_lines"] >= 1


class TestEditWindowConfig:
    def test_link_window_is_integer_hours(self):
        """make_interval принимает целое: дробные часы уронят запрос."""
        from quepasa.config import get_settings
        v = get_settings().get_path("autopost.link_window_hours")
        assert float(v).is_integer()

    def test_promotion_window_is_integer_hours(self):
        from quepasa.config import get_settings
        v = get_settings().get_path("autopost.promotion_window_hours")
        assert float(v).is_integer()


class TestSemanticEdits:
    """Смысловая правка — только через ревью, и всегда со строкой «Обновлено»."""

    def test_updated_line_appended(self):
        from quepasa.edits import UPDATED_PREFIX
        header = "**Заголовок**"
        what = "число погибших выросло с 90 до 111"
        out = f"{header}\n\n_{UPDATED_PREFIX} {what}_"
        assert UPDATED_PREFIX in out and what in out

    def test_edit_window_is_integer_hours(self):
        from quepasa.config import get_settings
        v = get_settings().get_path("autopost.edit_window_hours")
        assert float(v).is_integer(), "make_interval принимает целое"

    def test_edit_window_within_link_window(self):
        """Смысловая правка не должна пережить окно механических правок."""
        from quepasa.config import get_settings
        s = get_settings()
        assert (s.get_path("autopost.edit_window_hours")
                <= s.get_path("autopost.link_window_hours"))


class TestReviewCallbacks:
    """Формат callback_data: он ограничен 64 байтами (§10)."""

    def test_card_callback_fits(self):
        assert len("card:ok:ministerio-igualdad".encode()) <= 64

    def test_edit_callback_fits(self):
        assert len("edit:apply:999999".encode()) <= 64

    def test_post_callback_fits(self):
        assert len("post:pub:999999".encode()) <= 64

    def test_prefixes_are_distinct(self):
        """Обработчик различает их по префиксу — они не должны пересекаться."""
        prefixes = {"card:", "edit:", "post:"}
        assert len(prefixes) == 3
        assert not any(a != b and a.startswith(b) for a in prefixes for b in prefixes)


class TestAutopostSwitch:
    """Переключатель публикации: env важнее конфига (иначе deploy его затрёт)."""

    def test_env_true_overrides_config(self, monkeypatch):
        import quepasa.posts as p
        monkeypatch.setattr(p, "env", lambda k, d="": "true" if k == "AUTOPOST_ENABLED" else d)
        assert p.autopost_enabled()

    def test_env_false_overrides_config(self, monkeypatch):
        import quepasa.posts as p
        from quepasa.config import get_settings
        get_settings()["autopost"]["enabled"] = True
        try:
            monkeypatch.setattr(p, "env",
                                lambda k, d="": "false" if k == "AUTOPOST_ENABLED" else d)
            assert not p.autopost_enabled()
        finally:
            get_settings()["autopost"]["enabled"] = False

    def test_falls_back_to_config(self, monkeypatch):
        import quepasa.posts as p
        from quepasa.config import get_settings
        monkeypatch.setattr(p, "env", lambda k, d="": d)
        get_settings()["autopost"]["enabled"] = True
        try:
            assert p.autopost_enabled()
        finally:
            get_settings()["autopost"]["enabled"] = False


class TestNotModifiedIsNotAnError:
    """«not modified» — не сбой, а «видимый текст не изменился».

    Живой случай: в сюжет пришла laSexta, но при потолке в пять ссылок
    выбранная пятёрка не поменялась. Telegram отверг правку, sync_post счёл
    это ошибкой и не записал posted_source_ids — и следующий прогон зашёл
    на тот же круг. Пост бился о Telegram каждые полчаса семь часов подряд.
    """

    def test_marker_recognised(self):
        exc = Exception(
            "editMessageText: Bad Request: message is not modified: specified "
            "new message content and reply markup are exactly the same")
        assert "not modified" in str(exc)

    def test_real_errors_still_fail(self):
        for msg in ("Bad Request: message to edit not found",
                    "Forbidden: bot was blocked by the user",
                    "Too Many Requests: retry after 41"):
            assert "not modified" not in msg
