"""Markdown -> Telegram HTML и сборка поста.

Текст отсюда уходит в публичный канал: ошибка экранирования — это либо
отвергнутое сообщение, либо чужая разметка в нашем посте.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from quepasa.markup import markdown_to_telegram_html, strip_markdown  # noqa: E402
from quepasa.posts import (  # noqa: E402
    LEAN_EMOJI, compose_md, default_header_md, hashtag, render_links_md,
)


class TestMarkdown:
    def test_bold_italic_code(self):
        assert markdown_to_telegram_html("**жирный**") == "<b>жирный</b>"
        assert markdown_to_telegram_html("__курсив__") == "<i>курсив</i>"
        assert markdown_to_telegram_html("_курсив_") == "<i>курсив</i>"
        assert markdown_to_telegram_html("`код`") == "<code>код</code>"
        assert markdown_to_telegram_html("~~зачёркнуто~~") == "<s>зачёркнуто</s>"

    def test_link(self):
        out = markdown_to_telegram_html("[ABC](https://abc.es/x)")
        assert out == '<a href="https://abc.es/x">ABC</a>'

    def test_html_in_text_is_escaped(self):
        """Чужие теги не должны становиться разметкой."""
        out = markdown_to_telegram_html("Ley <b>rara</b> & cía")
        assert "&lt;b&gt;rara&lt;/b&gt;" in out
        assert "&amp;" in out

    def test_html_inside_link_label_is_escaped(self):
        out = markdown_to_telegram_html("[<script>x</script>](https://a.es)")
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_dangerous_scheme_is_not_a_link(self):
        out = markdown_to_telegram_html("[клик](javascript:alert(1))")
        assert "<a" not in out
        assert "javascript" in out  # остаётся текстом, но не ссылкой

    def test_quotes_in_url_escaped(self):
        out = markdown_to_telegram_html('[x](https://a.es/?q="1")')
        assert 'href="https://a.es/?q=&quot;1&quot;"' in out

    def test_markdown_not_applied_inside_code(self):
        out = markdown_to_telegram_html("`**не жирный**`")
        assert out == "<code>**не жирный**</code>"

    def test_underscore_inside_word_untouched(self):
        """some_var_name не должен превращаться в курсив."""
        assert markdown_to_telegram_html("some_var_name") == "some_var_name"

    def test_spanish_punctuation_survives(self):
        text = "¿Qué pasa? ¡Vaya! Precio: 1.234,56 € (aprox.) — sí"
        assert markdown_to_telegram_html(text) == text

    def test_empty(self):
        assert markdown_to_telegram_html("") == ""

    def test_multiline_bold(self):
        assert markdown_to_telegram_html("**две\nстроки**") == "<b>две\nстроки</b>"

    def test_strip_markdown(self):
        assert strip_markdown("**жирный** и [ссылка](https://a.es)") == "жирный и ссылка"


def art(source_id, name, lean, type_="press", title="Заголовок", url=None):
    return {
        "id": hash(source_id) % 1000,
        "source_id": source_id,
        "source_name": name,
        "lean": lean,
        "type": type_,
        "title": title,
        "url": url or f"https://{source_id}.es/a",
        "url_canonical": url or f"https://{source_id}.es/a",
        "published_at": datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc),
    }


class TestLinkBlock:
    def test_grouped_by_spectrum_in_order(self):
        arts = [
            art("abc", "ABC", "right"),
            art("eldiario", "elDiario.es", "left"),
            art("lavanguardia", "La Vanguardia", "center"),
        ]
        out = render_links_md(arts)
        assert out.count("\n") == 0, "издания идут одной строкой"
        # порядок — слева направо по шкале
        assert (out.index(LEAN_EMOJI["left"])
                < out.index(LEAN_EMOJI["center"])
                < out.index(LEAN_EMOJI["right"]))

    def test_same_lean_outlets_share_one_emoji(self):
        """«➡️ ABC · ➡️ OKdiario» — это один фланг, названный дважды."""
        arts = [art("abc", "ABC", "right"), art("okdiario", "OKdiario", "right")]
        out = render_links_md(arts)
        assert out.count("\n") == 0
        assert out.count(LEAN_EMOJI["right"]) == 1
        assert "ABC" in out and "OKdiario" in out

    def test_one_outlet_per_flank_unchanged(self):
        """Когда на значок приходится одно издание, вид прежний."""
        arts = [art("elespanol", "El Español", "center-right"),
                art("larazon", "La Razón", "right")]
        out = render_links_md(arts)
        assert out == ("▶️ [El Español](https://elespanol.es/a) · "
                       "➡️ [La Razón](https://larazon.es/a)")

    def test_official_goes_last_and_apart(self):
        arts = [art("abc", "ABC", "right"), art("boe", "BOE", "center", "official")]
        out = render_links_md(arts)
        # официальный источник — в конце и под своим значком, а не среди
        # центристских изданий: позиции в спектре у него нет
        assert out.index("🏛") > out.index("ABC")
        assert out.endswith("[BOE](https://boe.es/a)")

    def test_extreme_flanks_render(self):
        arts = [art("a", "A", "far-left"), art("b", "B", "far-right")]
        out = render_links_md(arts)
        assert out.startswith("⏪")
        assert "⏩" in out

    def test_article_without_url_skipped(self):
        arts = [art("abc", "ABC", "right")]
        arts[0]["url"] = arts[0]["url_canonical"] = None
        assert render_links_md(arts) == ""


class TestHashtag:
    @pytest.mark.parametrize(
        "cat,tag",
        [("политика", "#политика"), ("экономика", "#экономика"),
         ("культура/спорт", "#культура_и_спорт"), ("", "")],
    )
    def test_known(self, cat, tag):
        assert hashtag(cat) == tag

    def test_unknown_category_is_sanitised(self):
        """В хэштеге не должно быть пробелов и слэшей."""
        out = hashtag("моя странная/тема")
        assert out.startswith("#")
        assert " " not in out and "/" not in out


class TestCompose:
    def test_structure(self):
        arts = [art("abc", "ABC", "right"), art("eldiario", "elDiario.es", "left")]
        out = compose_md("**Заголовок**", arts, "политика")
        blocks = out.split("\n\n")
        assert blocks[0] == "**Заголовок**"
        assert blocks[-1] == "#политика"
        assert "elDiario.es" in out

    def test_header_survives_relink(self):
        """Ключевое: дополнение поста не должно затирать ручной текст."""
        header = "**Мой заголовок**\n\nМой текст."
        first = compose_md(header, [art("abc", "ABC", "right")], "политика")
        later = compose_md(
            header,
            [art("abc", "ABC", "right"), art("eldiario", "elDiario.es", "left")],
            "политика",
        )
        assert header in first and header in later
        assert "elDiario.es" in later and "elDiario.es" not in first

    def test_no_category_no_hashtag(self):
        out = compose_md("**Ф**", [art("abc", "ABC", "right")], "")
        assert "#" not in out

    def test_default_header_uses_newest_title(self):
        old = art("abc", "ABC", "right", title="Старый")
        new = art("eldiario", "elDiario.es", "left", title="Новый")
        new["published_at"] = datetime(2026, 8, 11, 23, 0, tzinfo=timezone.utc)
        assert "Новый" in default_header_md([old, new])

    def test_composed_post_is_valid_html(self):
        arts = [art("abc", "ABC & Cía", "right")]
        from quepasa.posts import compose_html
        out = compose_html("**Заголовок & <тест>**", arts, "политика")
        assert "&amp;" in out
        assert "<тест>" not in out


class TestLinkCap:
    """Ссылок в посте немного, и они показывают охват спектра."""

    def _many(self):
        from datetime import datetime, timezone
        spec = [("far-left","A"),("left","B"),("center-left","C"),("center","D"),
                ("center-right","E"),("right","F"),("far-right","G"),
                ("center","EP","agency"),("center","BOE","official")]
        out=[]
        for i,item in enumerate(spec):
            lean, owner = item[0], item[1]
            typ = item[2] if len(item)>2 else "press"
            out.append({"id":i,"source_id":f"s{i}","source_name":owner,"lean":lean,
                        "type":typ,"owner_group":owner,
                        "url":f"https://{owner}.es/a","url_canonical":f"https://{owner}.es/a",
                        "published_at":datetime(2026,8,11,tzinfo=timezone.utc)})
        return out

    def test_capped(self):
        """Потолок работает, когда он задан."""
        from quepasa.config import get_settings
        from quepasa.posts import pick_links
        s = get_settings()
        was = s["autopost"]["max_links_per_post"]
        s["autopost"]["max_links_per_post"] = 5
        try:
            assert len(pick_links(self._many())) == 5
        finally:
            s["autopost"]["max_links_per_post"] = was

    def test_covers_both_flanks(self):
        """Ключевое: в строке обязаны быть и левое, и правое."""
        from quepasa.spectrum import bucket
        from quepasa.posts import pick_links
        bs = {bucket(a["lean"]) for a in pick_links(self._many())
              if a["type"] == "press"}
        assert "left" in bs and "right" in bs

    def test_prefers_extremes_within_bucket(self):
        from quepasa.posts import pick_links
        names = {a["source_name"] for a in pick_links(self._many())}
        # крайние фланги важнее центристских соседей
        assert "A" in names and "G" in names

    def test_one_link_per_owner(self):
        from datetime import datetime, timezone
        from quepasa.posts import pick_links
        same = [{"id":i,"source_id":f"s{i}","source_name":f"Газета{i}","lean":"right",
                 "type":"press","owner_group":"Vocento",
                 "url":f"https://g{i}.es/a","url_canonical":f"https://g{i}.es/a",
                 "published_at":datetime(2026,8,11,tzinfo=timezone.utc)} for i in range(3)]
        assert len(pick_links(same)) == 1

    def test_agency_not_shown_as_centrist(self):
        from quepasa.posts import AGENCY_EMOJI, render_links_md
        md = render_links_md(self._many())
        assert AGENCY_EMOJI in md or "EP" not in md


class TestNoLinkCap:
    """Потолок 0 — показываем всех, кто написал."""

    @staticmethod
    def _arts(n):
        import datetime as dt
        leans = ["left", "center-left", "center", "center-right", "right"]
        return [{"id": i, "source_id": f"s{i}", "source_name": f"Издание{i}",
                 "lean": leans[i % 5], "type": "press", "owner_group": f"o{i}",
                 "url": f"https://s{i}.es/a", "url_canonical": f"https://s{i}.es/a",
                 "published_at": dt.datetime(2026, 8, 16)} for i in range(n)]

    @staticmethod
    def _with_limit(value, fn):
        from quepasa.config import get_settings
        s = get_settings()
        was = s["autopost"]["max_links_per_post"]
        s["autopost"]["max_links_per_post"] = value
        try:
            return fn()
        finally:
            s["autopost"]["max_links_per_post"] = was

    def test_zero_means_all(self):
        from quepasa.posts import pick_links
        arts = self._arts(12)
        assert len(self._with_limit(0, lambda: pick_links(arts))) == 12

    def test_positive_limit_still_caps(self):
        from quepasa.posts import pick_links
        arts = self._arts(12)
        assert len(self._with_limit(3, lambda: pick_links(arts))) == 3

    def test_config_ships_without_cap(self):
        """Владелец попросил показывать все ссылки."""
        from quepasa.config import get_settings
        assert get_settings().get_path("autopost.max_links_per_post") == 0


class TestWordFixes:
    """Устойчивые обмолвки модели по-русски.

    «Теннист Nick Kyrgios отстранён…» ушло в канал: слово стояло дважды
    в трёх предложениях, то есть модель ошибается в нём стабильно.
    """

    def test_typo_fixed_in_all_forms(self):
        from quepasa.posts import fix_names
        assert fix_names("Теннист отстранён") == "Теннисист отстранён"
        assert fix_names("австралийский теннист") == "австралийский теннисист"
        assert fix_names("двух теннистов") == "двух теннисистов"

    def test_correct_spelling_untouched(self):
        from quepasa.posts import fix_names
        assert fix_names("теннисист Nick Kyrgios") == "теннисист Nick Kyrgios"

    def test_other_words_untouched(self):
        from quepasa.posts import fix_names
        assert fix_names("теннисный турнир") == "теннисный турнир"
