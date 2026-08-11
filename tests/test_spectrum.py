"""Шкала, бакеты, размах и скор.

Здесь легко ошибиться молча: агентство, посчитанное центром, превращает
правило «фланг + центр» в «два любых источника».
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from quepasa import spectrum as sp  # noqa: E402


def src(lean, type_="press", owner=None, sid=None):
    return {"source_id": sid or f"{lean}-{owner or 'x'}", "lean": lean,
            "type": type_, "owner_group": owner or f"own-{lean}"}


class TestScale:
    @pytest.mark.parametrize("lean,val", [
        ("far-left", -3), ("left", -2), ("center-left", -1), ("center", 0),
        ("center-right", 1), ("right", 2), ("far-right", 3),
    ])
    def test_values(self, lean, val):
        assert sp.lean_value(lean) == val

    def test_unknown_label_is_none(self):
        assert sp.lean_value("выдуманное") is None

    @pytest.mark.parametrize("lean,b", [
        ("far-left", "left"), ("left", "left"), ("center-left", "left"),
        ("center", "center"),
        ("center-right", "right"), ("right", "right"), ("far-right", "right"),
    ])
    def test_buckets(self, lean, b):
        assert sp.bucket(lean) == b


class TestAgenciesAndOfficial:
    def test_agency_has_no_coordinate(self):
        """Агентство не даёт центра — иначе любое правило по спектру вырождается."""
        s = [src("left"), src("center", "agency")]
        assert sp.buckets(s) == {"left"}
        assert not sp.passes_rule(s)

    def test_official_has_no_coordinate(self):
        s = [src("left"), src("center", "official")]
        assert sp.buckets(s) == {"left"}

    def test_agency_still_counts_as_owner_in_score(self):
        """+1 к количеству оно даёт: подтверждает, что событие произошло."""
        assert sp.n_owners([src("left", owner="A"), src("center", "agency", owner="EP")]) == 2

    def test_agency_excluded_from_span(self):
        s = [src("left"), src("center", "agency"), src("center-left")]
        assert sp.span(s) == 1  # -2 и -1, центр агентства не считается


class TestOwners:
    def test_same_owner_counts_once(self):
        """Три газеты одного холдинга — одно подтверждение."""
        s = [src("right", owner="Vocento", sid="a"),
             src("right", owner="Vocento", sid="b"),
             src("right", owner="Vocento", sid="c")]
        assert sp.n_owners(s) == 1

    def test_fallback_to_source_id(self):
        assert sp.n_owners([{"source_id": "x", "lean": "center", "type": "press"}]) == 1


class TestSpan:
    def test_single_flank_is_zero(self):
        assert sp.span([src("left"), src("far-left")]) == 1
        assert sp.span([src("left")]) == 0

    def test_full_spectrum(self):
        assert sp.span([src("far-left"), src("far-right")]) == 6

    def test_empty(self):
        assert sp.span([]) == 0


class TestRule:
    def test_left_and_right(self):
        assert sp.passes_rule([src("left", owner="A"), src("right", owner="B")])

    def test_side_and_center(self):
        assert sp.passes_rule([src("left", owner="A"), src("center", owner="B")])

    def test_three_owners_one_flank(self):
        s = [src("right", owner=f"O{i}") for i in range(3)]
        assert sp.passes_rule(s)

    def test_two_of_one_flank_fails(self):
        s = [src("right", owner="A"), src("right", owner="B")]
        assert not sp.passes_rule(s)

    def test_three_outlets_one_owner_fails(self):
        """Ветка «≥3» считает владельцев, а не издания."""
        s = [src("right", owner="Vocento", sid=f"s{i}") for i in range(3)]
        assert not sp.passes_rule(s)


class TestOneSided:
    def test_single_bucket_is_one_sided(self):
        assert sp.is_one_sided([src("right", owner="A"), src("far-right", owner="B")])

    def test_two_buckets_is_not(self):
        assert not sp.is_one_sided([src("left"), src("right")])

    def test_agency_does_not_break_one_sidedness(self):
        """Агентство не является другой стороной."""
        assert sp.is_one_sided([src("right", owner="A"), src("center", "agency", owner="EP")])


class TestScore:
    def test_formula(self):
        """n_owners + 1.5 × span, без затухания при нулевом возрасте."""
        s = [src("far-left", owner="A"), src("far-right", owner="B")]
        assert sp.score(s, 0) == pytest.approx(2 + 1.5 * 6)

    def test_cross_flank_pair_beats_same_flank_pile(self):
        """Смысл W_SPAN: подтверждение оппонентом весит больше своего лагеря."""
        pair = [src("far-left", owner="A"), src("far-right", owner="B")]
        pile = [src("right", owner=f"O{i}") for i in range(6)]
        assert sp.score(pair, 0) > sp.score(pile, 0)

    def test_decay_halves_at_half_life(self):
        s = [src("left", owner="A"), src("right", owner="B")]
        assert sp.score(s, 12) == pytest.approx(sp.score(s, 0) / 2, rel=1e-6)

    def test_fresh_beats_stale_all_else_equal(self):
        s = [src("left", owner="A"), src("right", owner="B")]
        assert sp.score(s, 1) > sp.score(s, 24)


class TestPublishWindow:
    """Окно публикации и скользящая квота."""

    def _at(self, hour):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime(2026, 8, 11, hour, 0, tzinfo=ZoneInfo("Europe/Madrid"))

    def test_inside_window(self):
        from quepasa.posts import _in_publish_window
        assert _in_publish_window(self._at(9))
        assert _in_publish_window(self._at(20))

    def test_outside_window(self):
        from quepasa.posts import _in_publish_window
        assert not _in_publish_window(self._at(8))
        assert not _in_publish_window(self._at(21))
        assert not _in_publish_window(self._at(3))


class TestQuota:
    """Скользящее окно, а не календарные сутки."""

    class FakeConn:
        def __init__(self, used_24h, used_today):
            self.used_24h, self.used_today = used_24h, used_today
            self.calls = 0

        def execute(self, sql, params=None):
            self.calls += 1
            n = self.used_24h if "24 hours" in sql else self.used_today
            return type("R", (), {"fetchone": lambda self_: {"n": n}})()

    def _at(self, hour):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime(2026, 8, 11, hour, 0, tzinfo=ZoneInfo("Europe/Madrid"))

    def test_room_from_sliding_window(self):
        from quepasa.posts import _quota_state
        room, why = _quota_state(self.FakeConn(5, 5), self._at(19))
        assert room == 20 and "24 ч" in why

    def test_exhausted(self):
        from quepasa.posts import _quota_state
        room, _ = _quota_state(self.FakeConn(25, 25), self._at(19))
        assert room == 0

    def test_evening_reserve_binds_before_17(self):
        """До 17:00 нельзя выбрать больше 16, даже если суточная квота свободна."""
        from quepasa.posts import _quota_state
        room, why = _quota_state(self.FakeConn(16, 16), self._at(12))
        assert room == 0
        assert "резерв" in why

    def test_reserve_lifted_in_evening(self):
        from quepasa.posts import _quota_state
        room, _ = _quota_state(self.FakeConn(16, 16), self._at(18))
        assert room == 9
