"""Матчинг сущностей и правила показа (§7.3, §7.5).

Главное здесь — что нечёткое совпадение НЕ создаёт сущность: дубликат
расходится в фактах, и разгребать его дороже, чем завести руками.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from quepasa.entities import (  # noqa: E402
    normalize, pick_for_display, render_cards_html,
)


class TestNormalize:
    @pytest.mark.parametrize("raw,out", [
        ("Pedro Sánchez", "pedro sanchez"),
        ("PEDRO SANCHEZ", "pedro sanchez"),
        ("el PP", "pp"),
        ("президент Санчес", "санчес"),
        ("presidente Pedro Sánchez", "pedro sanchez"),
        ("La Moncloa", "moncloa"),
        ("  Junts  ", "junts"),
    ])
    def test_cases(self, raw, out):
        assert normalize(raw) == out

    def test_punctuation_dropped(self):
        assert normalize("¿Vox?") == "vox"

    def test_empty(self):
        assert normalize("") == ""
        assert normalize("   ") == ""


def ent(eid, *, status="approved", never=False, mentions=0, salience="secondary",
        last=None, card="Карточка"):
    return {"id": eid, "name_es": eid, "card": card, "card_status": status,
            "never_explain": never, "mentions_count": mentions,
            "salience": salience, "last_explained_at": last}


class FakeConn:
    """Кулдаун считается в SQL — подменяем ответ."""
    def __init__(self, days=999): self.days = days
    def execute(self, sql, params=None):
        return type("R", (), {"fetchone": lambda s: {"d": self.days}})()


class TestDisplayRules:
    def test_max_two_cards(self):
        ents = [ent(f"e{i}") for i in range(5)]
        assert len(pick_for_display(FakeConn(), ents)) == 2

    def test_draft_not_shown(self):
        assert pick_for_display(FakeConn(), [ent("a", status="draft")]) == []

    def test_never_explain_respected(self):
        assert pick_for_display(FakeConn(), [ent("a", never=True)]) == []

    def test_empty_card_not_shown(self):
        assert pick_for_display(FakeConn(), [ent("a", card="  ")]) == []

    def test_cooldown_blocks_recent(self):
        import datetime
        recent = datetime.datetime(2026, 8, 1)
        assert pick_for_display(FakeConn(days=3), [ent("a", last=recent)]) == []

    def test_cooldown_expired_allows(self):
        import datetime
        old = datetime.datetime(2026, 1, 1)
        assert len(pick_for_display(FakeConn(days=90), [ent("a", last=old)])) == 1

    def test_primary_before_secondary(self):
        ents = [ent("sec", salience="secondary"), ent("prim", salience="primary")]
        assert pick_for_display(FakeConn(), ents)[0]["id"] == "prim"

    def test_less_familiar_first_on_tie(self):
        """При равной значимости объясняем менее знакомое читателю."""
        ents = [ent("known", mentions=50), ent("rare", mentions=1)]
        assert pick_for_display(FakeConn(), ents)[0]["id"] == "rare"


class TestCardRender:
    def test_expandable_blockquote(self):
        html = render_cards_html([ent("x", card="Кто-то важный")])
        assert html.startswith("<blockquote expandable>")
        assert "Кто-то важный" in html

    def test_no_service_header(self):
        """Заголовка нет: в свёрнутом виде видна первая строка, и это должно
        быть пояснение, а не служебная подпись."""
        html = render_cards_html([ent("x", card="Пояснение")])
        assert "Кто это" not in html and "Что это" not in html
        assert html.startswith("<blockquote expandable><b>")

    def test_no_cards_no_block(self):
        """Пустой блок недопустим."""
        assert render_cards_html([]) == ""

    def test_html_escaped(self):
        html = render_cards_html([ent("x", card="A & <b>B</b>")])
        assert "&amp;" in html and "&lt;b&gt;" in html


class TestFuzzyHints:
    """Подсказка в очереди должна помогать, а не сбивать."""

    class Conn:
        def __init__(self, pairs): self.pairs = pairs
        def execute(self, sql, params=None):
            if "entity_aliases WHERE alias" in sql:
                key = params[0]
                hit = [e for e, a in self.pairs if a == key]
                return type("R", (), {
                    "fetchone": lambda s: {"entity_id": hit[0]} if hit else None})()
            return type("R", (), {"fetchall": lambda s: [
                {"entity_id": e, "alias": a} for e, a in self.pairs]})()

    def test_exact_match_wins(self):
        from quepasa.entities import match
        conn = self.Conn([("pp", "pp"), ("el-pais", "pais")])
        assert match(conn, "PP") == ("pp", None)

    def test_short_acronym_gets_no_bogus_hint(self):
        """ACS не должен «походить» на El País только из-за длины."""
        from quepasa.entities import match
        conn = self.Conn([("el-pais", "pais"), ("abc-diario", "abc")])
        assert match(conn, "ACS") == (None, None)

    def test_typo_in_long_name_hints(self):
        from quepasa.entities import match
        conn = self.Conn([("pedro-sanchez", "pedro sanchez")])
        eid, cand = match(conn, "Pedro Sanches")
        assert eid is None and cand == "pedro-sanchez"

    def test_surname_hint(self):
        from quepasa.entities import match
        conn = self.Conn([("pedro-sanchez", "pedro sanchez")])
        eid, cand = match(conn, "señor Sanchez")
        assert cand == "pedro-sanchez" or eid == "pedro-sanchez"

    def test_unknown_stays_unknown(self):
        from quepasa.entities import match
        conn = self.Conn([("pp", "pp")])
        assert match(conn, "Совершенно другое имя") == (None, None)


class TestContextMark:
    """Звёздочка у имени: без неё свёрнутый блок легко пропустить."""

    ENT = [{"name_es": "Florentino Pérez", "name_ru": "Флорентино Перес"}]

    def test_marks_name(self):
        from quepasa.entities import mark_entities
        assert mark_entities("Доля Florentino Pérez выросла", self.ENT) == \
            "Доля Florentino Pérez* выросла"

    def test_marks_russian_variant(self):
        from quepasa.entities import mark_entities
        out = mark_entities("Доля Флорентино Перес выросла", self.ENT)
        assert "Флорентино Перес*" in out

    def test_only_first_occurrence(self):
        from quepasa.entities import mark_entities
        out = mark_entities("Florentino Pérez и снова Florentino Pérez", self.ENT)
        assert out.count("*") == 1

    def test_absent_name_unchanged(self):
        from quepasa.entities import mark_entities
        assert mark_entities("Новость без имён", self.ENT) == "Новость без имён"

    def test_no_entities_unchanged(self):
        from quepasa.entities import mark_entities
        assert mark_entities("Текст", []) == "Текст"

    def test_does_not_break_bold_at_end(self):
        """Имя в конце жирного заголовка — самый опасный случай для markdown."""
        from quepasa.entities import mark_entities
        from quepasa.markup import markdown_to_telegram_html
        html = markdown_to_telegram_html(
            mark_entities("**Растёт доля Florentino Pérez**", self.ENT))
        assert html.count("<b>") == 1 and html.count("</b>") == 1
        assert "*" in html

    def test_does_not_break_bold_inside(self):
        from quepasa.entities import mark_entities
        from quepasa.markup import markdown_to_telegram_html
        html = markdown_to_telegram_html(
            mark_entities("**Florentino Pérez нарастил долю**", self.ENT))
        assert html == "<b>Florentino Pérez* нарастил долю</b>"

    def test_partial_word_not_marked(self):
        from quepasa.entities import mark_entities
        ents = [{"name_es": "ACS", "name_ru": ""}]
        assert mark_entities("Компания ACSA растёт", ents) == "Компания ACSA растёт"


class TestUnresolvedNotification:
    """Очередь — рабочий список владельца, а не служебная таблица."""

    class Conn:
        def __init__(self, rows): self.rows, self.updated = rows, False
        def execute(self, sql, params=None):
            if sql.strip().upper().startswith("UPDATE"):
                self.updated = True
                return type("R", (), {"fetchone": lambda s: None})()
            return type("R", (), {"fetchall": lambda s: self.rows})()

    def _row(self, surface, count=2, cand=None, urls=None):
        return {"surface": surface, "count": count, "candidate_id": cand,
                "sample_context": "", "sample_urls": urls or []}

    def test_sends_one_batched_message(self, monkeypatch):
        import quepasa.entities as en
        sent = []
        import quepasa.telegram as tg
        monkeypatch.setattr(tg, "notify_owner", lambda t, **k: sent.append(t))
        conn = self.Conn([self._row("galan"), self._row("infantino")])
        assert en.notify_new_unresolved(conn, min_count=2) == 2
        # одно сообщение, а не по штуке на сущность
        assert len(sent) == 1
        assert "galan" in sent[0] and "infantino" in sent[0]

    def test_includes_source_links(self, monkeypatch):
        """Без ссылки на новость решить, кто это, невозможно."""
        import quepasa.entities as en
        import quepasa.telegram as tg
        sent = []
        monkeypatch.setattr(tg, "notify_owner", lambda t, **k: sent.append(t))
        urls = [{"title": "Galán denuncia a Infantino", "source": "El Español",
                 "url": "https://elespanol.com/x"}]
        en.notify_new_unresolved(self.Conn([self._row("galan", urls=urls)]), min_count=2)
        assert "https://elespanol.com/x" in sent[0]
        assert "Galán denuncia" in sent[0]

    def test_includes_candidate_hint(self, monkeypatch):
        import quepasa.entities as en
        sent = []
        import quepasa.telegram as tg
        monkeypatch.setattr(tg, "notify_owner", lambda t, **k: sent.append(t))
        en.notify_new_unresolved(self.Conn([self._row("sanches", cand="pedro-sanchez")]),
                                 min_count=2)
        assert "pedro-sanchez" in sent[0]

    def test_silent_when_queue_empty(self, monkeypatch):
        import quepasa.entities as en
        sent = []
        import quepasa.telegram as tg
        monkeypatch.setattr(tg, "notify_owner", lambda t, **k: sent.append(t))
        assert en.notify_new_unresolved(self.Conn([]), min_count=2) == 0
        assert not sent

    def test_marks_as_notified(self, monkeypatch):
        import quepasa.entities as en
        import quepasa.telegram as tg
        monkeypatch.setattr(tg, "notify_owner", lambda t, **k: None)
        conn = self.Conn([self._row("x")])
        en.notify_new_unresolved(conn, min_count=2)
        assert conn.updated, "без отметки очередь пришлёт то же самое в следующий прогон"


class TestMeasureMode:
    """Замерный режим: кнопки вместо цитаты (§10)."""

    def _at(self, weekday):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        base = datetime(2026, 8, 10, 12, 0, tzinfo=ZoneInfo("Europe/Madrid"))  # пн
        return base + timedelta(days=weekday)

    def test_only_on_configured_weekday(self, monkeypatch):
        from quepasa.config import get_settings
        from quepasa.entities import is_measure_day
        s = get_settings()
        s["measure"]["enabled"] = True
        try:
            assert is_measure_day(self._at(2))       # среда
            assert not is_measure_day(self._at(1))
        finally:
            s["measure"]["enabled"] = False

    def test_disabled_by_default(self):
        from quepasa.entities import is_measure_day
        assert not is_measure_day(self._at(2))

    def test_callback_data_within_limit(self):
        from quepasa.entities import cards_keyboard
        kb = cards_keyboard([{"id": "florentino-perez", "name_es": "Florentino Pérez"}], 42)
        data = kb["inline_keyboard"][0][0]["callback_data"]
        assert len(data.encode()) <= 64
        assert data == "e:florentino-perez:42"

    def test_overlong_id_skipped_not_truncated(self):
        """Обрезать callback_data нельзя — обработчик не найдёт сущность."""
        from quepasa.entities import cards_keyboard
        kb = cards_keyboard([{"id": "x" * 70, "name_es": "Длинный"}], 1)
        assert kb is None

    def test_at_most_two_buttons(self):
        from quepasa.entities import cards_keyboard
        ents = [{"id": f"e{i}", "name_es": f"N{i}"} for i in range(5)]
        assert len(cards_keyboard(ents, 1)["inline_keyboard"][0]) == 2

    def test_no_entities_no_keyboard(self):
        from quepasa.entities import cards_keyboard
        assert cards_keyboard([], 1) is None


class TestCardNameDuplication:
    """Имя подставляется отдельно и ссылкой — в тексте его быть не должно."""

    def test_strips_leading_spanish_name(self):
        from quepasa.entities import strip_leading_name
        out = strip_leading_name("Margarita Robles — министр обороны Испании.",
                                 "Margarita Robles", "Маргарита Роблес")
        assert out == "Министр обороны Испании."

    def test_strips_leading_russian_name(self):
        from quepasa.entities import strip_leading_name
        out = strip_leading_name("Маргарита Роблес, министр обороны.",
                                 "Margarita Robles", "Маргарита Роблес")
        assert out == "Министр обороны."

    def test_leaves_card_without_name(self):
        from quepasa.entities import strip_leading_name
        card = "Министр обороны Испании."
        assert strip_leading_name(card, "Margarita Robles") == card

    def test_does_not_strip_name_in_middle(self):
        from quepasa.entities import strip_leading_name
        card = "Партия, которую возглавляет Margarita Robles."
        assert strip_leading_name(card, "Margarita Robles") == card

    def test_never_returns_empty(self):
        """Карточка из одного имени — лучше оставить как есть, чем обнулить."""
        from quepasa.entities import strip_leading_name
        assert strip_leading_name("Margarita Robles", "Margarita Robles") \
            == "Margarita Robles"

    def test_rendered_card_has_name_once(self):
        from quepasa.entities import render_cards_html
        html = render_cards_html([{
            "id": "x", "name_es": "Margarita Robles", "name_ru": "",
            "card": "Margarita Robles — министр обороны.", "wiki_url_es": None,
        }])
        assert html.count("Margarita Robles") == 1
