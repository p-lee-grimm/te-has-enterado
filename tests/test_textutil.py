"""Нормализация URL, дедуп заголовков и проверка на скрытое цитирование (§8.6)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from quepasa.textutil import (  # noqa: E402
    canonical_url, count_sentences, longest_common_shingle, normalize_title,
    shingles, strip_html, title_hash,
)


class TestCanonicalUrl:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            # utm и прочий трекинг снимаем
            ("https://elpais.com/a.html?utm_source=x&utm_medium=y",
             "https://elpais.com/a.html"),
            ("https://elpais.com/a.html?fbclid=abc", "https://elpais.com/a.html"),
            ("https://elpais.com/a.html?gclid=1&id=7", "https://elpais.com/a.html?id=7"),
            # www и amp — то же самое издание
            ("https://www.abc.es/x", "https://abc.es/x"),
            ("https://amp.abc.es/x", "https://abc.es/x"),
            ("https://abc.es/x/amp", "https://abc.es/x"),
            # фрагмент и хвостовой слэш не влияют на тождество
            ("https://abc.es/x#comentarios", "https://abc.es/x"),
            ("https://abc.es/x/", "https://abc.es/x"),
            # схема и хост к нижнему регистру, путь — нет
            ("HTTPS://ABC.ES/Ruta", "https://abc.es/Ruta"),
            # порядок значимых параметров не должен создавать разные ключи
            ("https://abc.es/x?b=2&a=1", "https://abc.es/x?a=1&b=2"),
            ("https://abc.es//dobles//slashes", "https://abc.es/dobles/slashes"),
        ],
    )
    def test_canonicalisation(self, raw, expected):
        assert canonical_url(raw) == expected

    def test_same_article_different_tracking_collapses(self):
        """Ровно то, ради чего нужен дедуп: один материал из двух перезаливов фида."""
        a = canonical_url("https://www.eldiario.es/n.html?utm_campaign=rss#top")
        b = canonical_url("https://eldiario.es/n.html/")
        assert a == b

    def test_different_articles_stay_different(self):
        assert canonical_url("https://abc.es/a") != canonical_url("https://abc.es/b")

    def test_empty_and_garbage(self):
        assert canonical_url("") == ""
        assert canonical_url("   ") == ""
        # не-http схему не трогаем
        assert canonical_url("mailto:a@b.c") == "mailto:a@b.c"

    def test_is_idempotent(self):
        once = canonical_url("https://www.abc.es/x/?utm_source=q#f")
        assert canonical_url(once) == once


class TestTitleDedup:
    def test_case_and_punctuation_ignored(self):
        assert normalize_title("El Gobierno APRUEBA la ley!") == normalize_title(
            "el gobierno aprueba la ley"
        )

    def test_diacritics_ignored(self):
        assert normalize_title("Peña y Móstoles") == normalize_title("Pena y Mostoles")

    def test_outlet_tail_stripped(self):
        assert normalize_title("Sánchez comparece | El País") == normalize_title(
            "Sánchez comparece"
        )

    def test_hash_stable_and_short(self):
        h = title_hash("El Gobierno aprueba la ley")
        assert h == title_hash("el gobierno aprueba la ley!")
        assert len(h) == 32

    def test_different_titles_differ(self):
        assert title_hash("Incendio en Huelva") != title_hash("Incendio en Madrid")


class TestQuotationCheck:
    """Механическая проверка §3.10 — окно в N слов подряд."""

    def test_finds_verbatim_run(self):
        source = (
            "El presidente del Gobierno anunció esta mañana que el plan de vivienda "
            "se aplicará a partir de enero en todas las comunidades autónomas."
        )
        stolen = (
            "Segun el texto, el plan de vivienda se aplicara a partir de enero en todas "
            "las comunidades autonomas."
        )
        assert longest_common_shingle(stolen, source, 10) is not None

    def test_paraphrase_passes(self):
        source = (
            "El presidente del Gobierno anunció esta mañana que el plan de vivienda "
            "se aplicará a partir de enero en todas las comunidades autónomas."
        )
        ours = (
            "Правительство утвердило жилищный план. Он начнёт действовать с января "
            "во всех автономных сообществах."
        )
        assert longest_common_shingle(ours, source, 10) is None

    def test_short_overlap_allowed(self):
        """Короткое совпадение — неизбежность языка, а не цитата."""
        source = "El Congreso de los Diputados aprobó la reforma con amplia mayoría."
        ours = "El Congreso de los Diputados votó otra cosa distinta por completo hoy."
        assert longest_common_shingle(ours, source, 10) is None

    def test_window_size_respected(self):
        source = "uno dos tres cuatro cinco seis siete ocho nueve diez once doce"
        ours = "uno dos tres cuatro cinco seis siete ocho nueve diez"
        assert longest_common_shingle(ours, source, 10) is not None
        assert longest_common_shingle(ours, source, 11) is None

    def test_shingles_edge_cases(self):
        assert shingles([], 3) == set()
        assert shingles(["a", "b"], 3) == set()
        assert shingles(["a", "b", "c"], 3) == {"a b c"}


class TestMisc:
    @pytest.mark.parametrize(
        "text,n",
        [("", 0), ("Una frase.", 1), ("Una. Dos. Tres.", 3), ("¿Qué? ¡Vaya! Sí.", 3),
         ("Sin punto final", 1)],
    )
    def test_count_sentences(self, text, n):
        assert count_sentences(text) == n

    def test_strip_html(self):
        assert strip_html("<p>Hola <b>mundo</b></p>") == "Hola mundo"
        assert strip_html("<script>bad()</script>texto") == "texto"
        assert strip_html("a &amp; b") == "a & b"
