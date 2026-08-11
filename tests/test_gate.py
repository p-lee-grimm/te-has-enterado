"""Логика ворот качества и вёрстки (§8.6).

Проверки, которые ходят в сеть или в БД, здесь не запускаются — тестируется
именно логика решения «публиковать / не публиковать».
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quepasa.stages.gate import (  # noqa: E402
    GateReport, _check_fields, _check_link_diversity, _check_quotations,
)
from quepasa.stages.render import pick_links, render  # noqa: E402
from quepasa.stages.select import enforce_topic_diversity, is_repeat_eligible  # noqa: E402


def make_item(**over):
    item = {
        "cluster_id": 1,
        "headline": "Правительство подняло минимальную зарплату",
        "summary": "Совет министров утвердил повышение на четыре процента. "
                   "Профсоюзы поддержали. Работодатели отказались подписывать.",
        "context": "SMI — нижний порог оплаты труда, его пересматривают раз в год.",
        "framing": "",
        "topic": "экономика",
        "confidence": "high",
        "_links": [
            {"url": "https://eldiario.es/a", "source_id": "eldiario",
             "source_name": "elDiario.es", "lean": "left"},
            {"url": "https://abc.es/b", "source_id": "abc",
             "source_name": "ABC", "lean": "right"},
        ],
        "all_articles": [],
    }
    item.update(over)
    return item


class TestFieldChecks:
    def test_good_item_passes(self):
        r = GateReport()
        _check_fields([make_item()], r)
        assert r.passed

    def test_empty_summary_fails(self):
        r = GateReport()
        _check_fields([make_item(summary="")], r)
        assert not r.passed

    def test_empty_context_fails(self):
        r = GateReport()
        _check_fields([make_item(context="")], r)
        assert not r.passed

    def test_too_many_sentences_fails(self):
        r = GateReport()
        _check_fields([make_item(summary="Раз. Два. Три. Четыре. Пять.")], r)
        assert not r.passed
        assert "предложений" in r.failures[0].detail

    def test_truncated_text_fails(self):
        """Обрыв на полуслове — признак срезанного ответа модели."""
        r = GateReport()
        _check_fields([make_item(summary="Совет министров утвердил повышение и затем")], r)
        assert not r.passed
        assert "обрывается" in r.failures[0].detail

    def test_item_without_links_fails(self):
        r = GateReport()
        _check_fields([make_item(_links=[])], r)
        assert not r.passed


class TestLinkDiversity:
    def test_two_different_outlets_pass(self):
        r = GateReport()
        _check_link_diversity([make_item()], r)
        assert r.passed

    def test_single_link_fails(self):
        r = GateReport()
        _check_link_diversity([make_item(_links=[
            {"url": "https://abc.es/b", "source_id": "abc", "source_name": "ABC", "lean": "right"},
        ])], r)
        assert not r.passed

    def test_same_outlet_twice_fails(self):
        r = GateReport()
        dup = [
            {"url": "https://abc.es/1", "source_id": "abc", "source_name": "ABC", "lean": "right"},
            {"url": "https://abc.es/2", "source_id": "abc", "source_name": "ABC", "lean": "right"},
        ]
        _check_link_diversity([make_item(_links=dup)], r)
        assert not r.passed


class TestQuotationGate:
    def test_verbatim_quote_blocks_digest(self):
        source_article = {
            "body": "El Consejo de Ministros aprobó este martes una subida del salario "
                    "mínimo interprofesional del cuatro por ciento para todos los trabajadores.",
            "summary_feed": "",
        }
        item = make_item(
            summary="El Consejo de Ministros aprobo este martes una subida del salario "
                    "minimo interprofesional del cuatro por ciento para todos.",
            all_articles=[source_article],
        )
        r = GateReport()
        _check_quotations([item], r)
        assert not r.passed

    def test_own_words_pass(self):
        source_article = {
            "body": "El Consejo de Ministros aprobó este martes una subida del salario "
                    "mínimo interprofesional del cuatro por ciento.",
            "summary_feed": "",
        }
        r = GateReport()
        _check_quotations([make_item(all_articles=[source_article])], r)
        assert r.passed


class TestGateReport:
    def test_one_failure_fails_everything(self):
        r = GateReport()
        r.add("a", True, "ок")
        r.add("b", False, "плохо")
        r.add("c", True, "ок")
        assert not r.passed
        assert len(r.failures) == 1
        assert "b" in r.reason()

    def test_serialisable(self):
        r = GateReport()
        r.add("a", True, "ок")
        assert r.as_dict()["passed"] is True
        assert r.as_dict()["checks"][0]["name"] == "a"


class TestRepeatRule:
    def test_never_published_included_as_new(self):
        include, cont = is_repeat_eligible({"last_published_at": None, "n_articles": 5})
        assert include and not cont

    def test_published_without_updates_excluded(self):
        from datetime import datetime, timedelta, timezone
        cluster = {
            "last_published_at": datetime.now(timezone.utc) - timedelta(hours=10),
            "n_articles": 6,
            "n_articles_at_publish": 6,
        }
        include, cont = is_repeat_eligible(cluster)
        assert not include and cont

    def test_enough_new_articles_returns_as_continuation(self):
        from datetime import datetime, timedelta, timezone
        cluster = {
            "last_published_at": datetime.now(timezone.utc) - timedelta(hours=10),
            "n_articles": 9,
            "n_articles_at_publish": 6,
        }
        include, cont = is_repeat_eligible(cluster)
        assert include and cont

    def test_long_gap_with_ongoing_flow_returns(self):
        from datetime import datetime, timedelta, timezone
        cluster = {
            "last_published_at": datetime.now(timezone.utc) - timedelta(hours=80),
            "n_articles": 7,
            "n_articles_at_publish": 6,
        }
        include, cont = is_repeat_eligible(cluster)
        assert include and cont


class TestTopicDiversity:
    def test_four_in_a_row_gets_deferred(self):
        items = [make_item(topic="политика", headline=f"h{i}") for i in range(4)]
        items.append(make_item(topic="экономика", headline="эконом"))
        out = enforce_topic_diversity(items)
        assert [i["topic"] for i in out[:3]] == ["политика"] * 3
        assert out[3]["topic"] == "экономика"
        assert len(out) == 5

    def test_mixed_order_preserved(self):
        items = [
            make_item(topic="политика"), make_item(topic="экономика"),
            make_item(topic="политика"),
        ]
        assert [i["topic"] for i in enforce_topic_diversity(items)] == [
            "политика", "экономика", "политика",
        ]


class TestRender:
    def _articles(self):
        return [
            {"url": "https://eldiario.es/a", "url_canonical": "https://eldiario.es/a",
             "source_id": "eldiario", "source_name": "elDiario.es", "lean": "left"},
            {"url": "https://infolibre.es/b", "url_canonical": "https://infolibre.es/b",
             "source_id": "infolibre", "source_name": "infoLibre", "lean": "left"},
            {"url": "https://abc.es/c", "url_canonical": "https://abc.es/c",
             "source_id": "abc", "source_name": "ABC", "lean": "right"},
            {"url": "https://lavanguardia.com/d", "url_canonical": "https://lavanguardia.com/d",
             "source_id": "lavanguardia", "source_name": "La Vanguardia", "lean": "center"},
        ]

    def test_links_span_different_poles(self):
        """§3.9 — не три левых подряд."""
        picked = pick_links(self._articles(), 3)
        assert len(picked) == 3
        assert len({p["lean"] for p in picked}) >= 3

    def test_one_link_per_outlet(self):
        dupes = self._articles() + [
            {"url": "https://abc.es/e", "url_canonical": "https://abc.es/e",
             "source_id": "abc", "source_name": "ABC", "lean": "right"},
        ]
        picked = pick_links(dupes, 3)
        assert len({p["source_id"] for p in picked}) == len(picked)

    def test_post_structure(self):
        item = make_item(all_articles=self._articles(), framing="Левые пишут одно, правые другое.")
        messages = render([item])
        assert len(messages) == 1
        text = messages[0]
        assert "📅 Испания," in text
        assert "<b>" in text and "<a href=" in text
        assert "<i>Контекст:</i>" in text
        assert "<i>Как подают:</i>" in text

    def test_low_confidence_marked(self):
        item = make_item(confidence="low", all_articles=self._articles())
        assert "источники расходятся" in render([item])[0]

    def test_continuation_marked(self):
        item = make_item(is_continuation=True, all_articles=self._articles())
        assert "продолжение" in render([item])[0]

    def test_html_is_escaped(self):
        item = make_item(headline="Ley <b>rara</b> & cía", all_articles=self._articles())
        text = render([item])[0]
        assert "&lt;b&gt;rara&lt;/b&gt;" in text and "&amp;" in text

    def test_long_digest_splits_on_item_boundary(self):
        """Длинный пост режем по границе пункта, не обрывая текст (§3.9)."""
        long_summary = ("Очень длинное предложение о событии дня в Испании. " * 40).strip()
        items = [
            make_item(summary=long_summary, headline=f"Пункт {i}", all_articles=self._articles())
            for i in range(5)
        ]
        messages = render(items)
        assert len(messages) > 1
        assert all(len(m) <= 4096 for m in messages)
        # каждый пункт целиком в одном сообщении: считаем вхождения заголовков
        joined = "\n".join(messages)
        for i in range(5):
            assert f"Пункт {i}" in joined

    def test_separator_between_items_only(self):
        """Разделитель разделяет пункты, а не открывает пост (§3.9)."""
        from quepasa.stages.render import SEPARATOR
        items = [make_item(headline=f"Пункт {i}", all_articles=self._articles())
                 for i in range(3)]
        text = render(items)[0]
        assert text.count(SEPARATOR) == 2
        assert not text.split("\n\n")[1].startswith(SEPARATOR)

    def test_empty_framing_omits_block(self):
        item = make_item(framing="", all_articles=self._articles())
        assert "Как подают" not in render([item])[0]


class TestSchedule:
    """Переход на летнее время не должен сдвигать выпуск (§3.11)."""

    def _at(self, iso):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime.fromisoformat(iso).replace(tzinfo=ZoneInfo("UTC"))

    def test_summer_utc_hour(self):
        from quepasa.schedule import should_run_now
        # Вечерний пост в 21:30 по Мадриду; CEST (UTC+2) => 19:30 UTC
        assert should_run_now(self._at("2026-08-11T19:30:00"))[0]
        assert not should_run_now(self._at("2026-08-11T20:30:00"))[0]

    def test_winter_utc_hour(self):
        from quepasa.schedule import should_run_now
        # CET (UTC+1) => 20:30 UTC. Хардкодить UTC-час нельзя: полгода
        # выпуск выходил бы на час не вовремя.
        assert should_run_now(self._at("2026-01-15T20:30:00"))[0]
        assert not should_run_now(self._at("2026-01-15T19:30:00"))[0]

    def test_late_start_within_tolerance(self):
        from quepasa.schedule import should_run_now
        assert should_run_now(self._at("2026-08-11T19:45:00"))[0]

    def test_way_off_rejected(self):
        from quepasa.schedule import should_run_now
        assert not should_run_now(self._at("2026-08-11T09:00:00"))[0]
