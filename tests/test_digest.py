"""Дайджест: группировка по категориям и вёрстка."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from quepasa.config import get_settings  # noqa: E402
from quepasa.digest import OTHER, group_by_category, render_md  # noqa: E402


@pytest.fixture
def merge_at(request):
    """Порог слияния мелких категорий берём явно, а не из живого конфига."""
    s = get_settings()
    old = s["digest"]["min_items_per_category"]
    s["digest"]["min_items_per_category"] = getattr(request, "param", 3)
    yield
    s["digest"]["min_items_per_category"] = old


def item(topic, headline="Заголовок", arts=None):
    return {
        "cluster_id": 1, "topic": topic, "headline": headline,
        "articles": arts if arts is not None else [{
            "source_id": "abc", "source_name": "ABC", "lean": "right",
            "type": "press", "url": "https://abc.es/a", "url_canonical": "https://abc.es/a",
        }],
    }


class TestGrouping:
    def test_big_categories_kept_separate(self, merge_at):
        items = [item("политика") for _ in range(3)] + [item("экономика") for _ in range(3)]
        g = group_by_category(items)
        assert set(g) == {"политика", "экономика"}

    def test_small_categories_merged_into_other(self, merge_at):
        """Категория из одного пункта не должна занимать свой заголовок."""
        items = [item("политика") for _ in range(3)] + [item("регионы"), item("культура/спорт")]
        g = group_by_category(items)
        assert "политика" in g
        assert "регионы" not in g and "культура/спорт" not in g
        assert len(g[OTHER]) == 2

    def test_all_small_collapses_to_single_block(self, merge_at):
        items = [item("политика"), item("экономика"), item("регионы")]
        g = group_by_category(items)
        assert list(g) == [OTHER]
        assert len(g[OTHER]) == 3

    def test_category_order_is_stable(self, merge_at):
        items = ([item("культура/спорт")] * 3 + [item("политика")] * 3
                 + [item("экономика")] * 3)
        # политика идёт раньше экономики, экономика раньше культуры
        assert list(group_by_category(items)) == ["политика", "экономика", "культура/спорт"]

    def test_missing_topic_goes_to_other(self, merge_at):
        g = group_by_category([item(""), item(""), item("")])
        assert list(g) == [OTHER]

    def test_other_is_always_last(self, merge_at):
        items = [item("политика")] * 3 + [item("регионы")]
        assert list(group_by_category(items))[-1] == OTHER


class TestRender:
    def test_has_header_categories_and_hashtag(self):
        items = [item("политика", f"Новость {i}") for i in range(3)]
        md = render_md(items, "11 августа")
        assert md.startswith("**Коротко — 11 августа**")
        assert "**Политика**" in md
        assert md.rstrip().endswith("#дайджест")
        assert "Новость 0" in md and "Новость 2" in md

    def test_source_marks_use_spectrum_emoji(self):
        arts = [
            {"source_id": "eldiario", "source_name": "elDiario.es", "lean": "left",
             "type": "press", "url": "https://e.es/a", "url_canonical": "https://e.es/a"},
            {"source_id": "abc", "source_name": "ABC", "lean": "right",
             "type": "press", "url": "https://a.es/b", "url_canonical": "https://a.es/b"},
        ]
        md = render_md([item("политика", "Н", arts)] * 3, "11 августа")
        assert "⬅️" in md and "➡️" in md

    def test_official_source_marked_apart(self):
        arts = [{"source_id": "boe", "source_name": "BOE", "lean": "center",
                 "type": "official", "url": "https://boe.es/x",
                 "url_canonical": "https://boe.es/x"}]
        md = render_md([item("политика", "Н", arts)] * 3, "11 августа")
        assert "🏛" in md

    def test_article_without_url_skipped(self):
        arts = [{"source_id": "abc", "source_name": "ABC", "lean": "right",
                 "type": "press", "url": None, "url_canonical": None}]
        md = render_md([item("политика", "Н", arts)] * 3, "11 августа")
        assert "ABC" not in md


class TestSound:
    """Звук: только крупный сюжет и не чаще квоты."""

    def _row(self, n):
        return {"n_sources": n}

    def _patch(self, monkeypatch, quota=5, percentile=None):
        import quepasa.posts as p
        monkeypatch.setattr(p, "sound_quota_left", lambda conn: quota)
        monkeypatch.setattr(p, "score_percentile", lambda conn, pct, days: percentile)
        return p

    def test_small_story_is_silent(self, monkeypatch):
        p = self._patch(monkeypatch)
        assert not p.deserves_sound(None, self._row(3))

    def test_big_story_gets_sound(self, monkeypatch):
        p = self._patch(monkeypatch)
        assert p.deserves_sound(None, self._row(6))

    def test_percentile_used_when_history_exists(self, monkeypatch):
        """Есть история — решает скор, а не число изданий."""
        p = self._patch(monkeypatch, percentile=10.0)
        assert p.deserves_sound(None, {"n_sources": 2, "score": 12.0})
        assert not p.deserves_sound(None, {"n_sources": 9, "score": 3.0})

    def test_falls_back_without_history(self, monkeypatch):
        p = self._patch(monkeypatch, percentile=None)
        assert p.deserves_sound(None, {"n_sources": 9, "score": 3.0})

    def test_quota_exhausted_forces_silence(self, monkeypatch):
        """Даже крупный сюжет молчит, если дневная квота выбрана."""
        p = self._patch(monkeypatch, quota=0, percentile=1.0)
        assert not p.deserves_sound(None, {"n_sources": 11, "score": 99.0})

    def test_disabled_globally(self, monkeypatch):
        from quepasa.config import get_settings
        p = self._patch(monkeypatch)
        get_settings()["autopost"]["sound"]["enabled"] = False
        try:
            assert not p.deserves_sound(None, self._row(11))
        finally:
            get_settings()["autopost"]["sound"]["enabled"] = True

    def test_send_message_passes_silent_flag(self, monkeypatch):
        import quepasa.telegram as tg
        captured = {}
        monkeypatch.setattr(tg, "_call", lambda m, p: captured.update(p) or {"message_id": 1})
        tg.send_message("@c", "текст", silent=True)
        assert captured["disable_notification"] is True
        tg.send_message("@c", "текст", silent=False)
        assert captured["disable_notification"] is False


class TestLinkCap:
    """Строка дайджеста обязана влезать в лимит Telegram."""

    def _arts(self, n):
        leans = ["far-left", "left", "center-left", "center",
                 "center-right", "right", "far-right"]
        return [{"source_id": f"s{i}", "source_name": f"Издание{i}",
                 "lean": leans[i % len(leans)], "type": "press",
                 # реальные URL длинные, и именно на них пост переставал влезать
                 "url": f"https://ejemplo{i}.es/politica/20260810/"
                        f"noticia-con-titular-largo-{i}-133240444"
                        f"?utm_source=rss-noticias&utm_medium=feed&utm_campaign=politica",
                 "url_canonical": f"https://ejemplo{i}.es/a"}
                for i in range(n)]

    def test_caps_number_of_links(self):
        from quepasa.digest import _source_marks
        md = _source_marks(self._arts(11))
        assert md.count("https://") == 2

    def test_keeps_spectrum_edges(self):
        """Берём крайних: строка должна показывать размах."""
        from quepasa.digest import _source_marks
        md = _source_marks(self._arts(7))
        assert "⏪" in md and "⏩" in md

    def test_fewer_than_limit_all_shown(self):
        from quepasa.digest import _source_marks
        assert _source_marks(self._arts(2)).count("https://") == 2

    def _long_items(self, n):
        return [{"cluster_id": i, "topic": "политика",
                 "headline": "Достаточно длинный заголовок новости про Испанию " * 2,
                 "articles": self._arts(11)} for i in range(n)]

    def test_twelve_items_with_real_urls_overflow(self):
        """Без обрезки двенадцать пунктов с настоящими URL не влезают."""
        from quepasa.digest import render_md
        from quepasa.markup import markdown_to_telegram_html
        html = markdown_to_telegram_html(render_md(self._long_items(12), "10 августа"))
        assert len(html) > 4096

    def test_splits_into_chain_instead_of_dropping(self):
        """Длинный дайджест уходит цепочкой, а не теряет пункты молча."""
        from quepasa.digest import split_messages
        from quepasa.markup import markdown_to_telegram_html
        parts = split_messages(self._long_items(12), "10 августа")
        assert len(parts) > 1
        for p in parts:
            assert len(markdown_to_telegram_html(p)) <= 4096

    def test_short_digest_is_one_message(self):
        from quepasa.digest import split_messages
        assert len(split_messages(self._long_items(3), "10 августа")) == 1

    def test_chain_capped_by_max_messages(self):
        from quepasa.digest import split_messages
        parts = split_messages(self._long_items(60), "10 августа")
        assert len(parts) <= 3

    def test_main_block_only_in_first_message(self):
        from quepasa.digest import split_messages
        parts = split_messages(self._long_items(12), "10 августа",
                               main_block="**Главное за сегодня**\n\n• [x](https://t.me/c/1)")
        assert "Главное за сегодня" in parts[0]
        assert all("Главное за сегодня" not in p for p in parts[1:])


class TestMainBlock:
    def test_links_point_into_channel(self):
        from quepasa.digest import render_main_block
        md = render_main_block(
            [{"header_md": "**Заголовок поста**", "message_id": 42, "cluster_id": 1}],
            "@tehasenterado")
        assert "https://t.me/tehasenterado/42" in md
        assert "Заголовок поста" in md

    def test_empty_when_no_posts(self):
        from quepasa.digest import render_main_block
        assert render_main_block([], "@c") == ""

    def test_headline_shortened_on_word_boundary(self):
        from quepasa.digest import shorten
        out = shorten("Правительство одобрило повышение минимальной зарплаты на четыре процента", 30)
        assert len(out) <= 31 and out.endswith("…")
        assert not out[:-1].endswith(" ")

    def test_short_headline_untouched(self):
        from quepasa.digest import shorten
        assert shorten("Короткий", 60) == "Короткий"
