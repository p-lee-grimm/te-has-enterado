"""Ворота качества отдельного поста (§8).

Здесь важнее всего не пропустить ложные срабатывания: ворота, которые режут
нормальные посты, владелец через неделю отключит целиком.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quepasa.postgate import GateReport, _check_fields, _check_geo_tag  # noqa: E402
from quepasa.postgate import _check_one_sided_line, _check_scope  # noqa: E402
from quepasa.posts import ONE_SIDED_LINE  # noqa: E402


class TestFields:
    def test_normal_post_passes(self):
        r = GateReport()
        _check_fields(r, "Правительство повысило минимальную зарплату",
                      "Совет министров утвердил новый размер.", "")
        assert r.passed, r.reason()

    def test_headline_without_period_is_fine(self):
        """Заголовок точкой не заканчивается — это норма, а не обрыв."""
        r = GateReport()
        _check_fields(r, "Араухо перешёл в Ливерпуль в аренду", "", "")
        assert r.passed, r.reason()

    def test_headline_with_period_rejected(self):
        r = GateReport()
        _check_fields(r, "Заголовок с точкой.", "", "")
        assert not r.passed

    def test_empty_lead_allowed(self):
        """Если заголовок объясняет всё, пустой lead — правильный ответ."""
        r = GateReport()
        _check_fields(r, "В Японии прошли парламентские выборы", "", "")
        assert r.passed

    def test_empty_headline_rejected(self):
        r = GateReport()
        _check_fields(r, "", "текст", "")
        assert not r.passed

    def test_significance_without_period_allowed(self):
        """significance — фраза, а не предложение: точка необязательна."""
        r = GateReport()
        _check_fields(r, "Заголовок", "",
                      "Важно для тех, кто следит за кризисом безопасности Ceuta")
        assert r.passed, r.reason()

    def test_truncated_summary_rejected(self):
        r = GateReport()
        _check_fields(r, "Заголовок",
                      "Совет министров утвердил повышение. Профсоюзы затем", "")
        assert not r.passed
        assert "обрывается" in r.reason()

    def test_short_fragment_not_flagged_as_truncated(self):
        """Короткий хвост без точки — не обрыв; иначе ложных срабатываний море."""
        r = GateReport()
        _check_fields(r, "Заголовок", "Да", "")
        assert r.passed

    def test_too_many_sentences(self):
        r = GateReport()
        _check_fields(r, "Заголовок", "Раз. Два. Три. Четыре. Пять.", "")
        assert not r.passed


class TestScope:
    def test_world_blocked(self):
        r = GateReport()
        _check_scope(r, "world")
        assert not r.passed

    def test_world_linked_allowed(self):
        r = GateReport()
        _check_scope(r, "world_linked")
        assert r.passed

    def test_missing_scope_skips(self):
        r = GateReport()
        _check_scope(r, None)
        assert r.passed


class TestGeoTag:
    def test_known_tag(self):
        r = GateReport()
        _check_geo_tag(r, "#Каталония")
        assert r.passed

    def test_unknown_tag_blocked(self):
        r = GateReport()
        _check_geo_tag(r, "#Выдуманное")
        assert not r.passed

    def test_no_tag_is_fine(self):
        r = GateReport()
        _check_geo_tag(r, None)
        assert r.passed


class TestOneSided:
    def test_flag_requires_line(self):
        r = GateReport()
        _check_one_sided_line(r, True, "Заголовок\n\nссылки")
        assert not r.passed

    def test_line_present(self):
        r = GateReport()
        _check_one_sided_line(r, True, f"Заголовок\n\n{ONE_SIDED_LINE}\n\nссылки")
        assert r.passed

    def test_not_one_sided_needs_nothing(self):
        r = GateReport()
        _check_one_sided_line(r, False, "Заголовок")
        assert r.passed


class TestJunkFilter:
    """Ежедневные рубрики проходят любую ветку допуска, но новостью не являются."""

    def test_lottery_variants(self):
        from quepasa.posts import is_junk
        for t in ["Cupón diario de la ONCE: comprobar sorteo",
                  "Bonoloto: comprobar el resultado del sorteo de hoy",
                  "La Primitiva de hoy | Comprobar resultado del sorteo",
                  "Euromillones: números premiados",
                  "Horóscopo de hoy, lunes 10 de agosto"]:
            assert is_junk([t]), t

    def test_real_news_passes(self):
        from quepasa.posts import is_junk
        for t in ["El Gobierno aprueba la subida del salario mínimo",
                  "Fuera de control uno de los peores incendios de Andalucía",
                  "Marruecos exige la repatriación de sus menores"]:
            assert not is_junk([t]), t

    def test_any_title_in_cluster_marks_it(self):
        from quepasa.posts import is_junk
        assert is_junk(["Noticia normal", "Bonoloto: comprobar el sorteo"])

    def test_empty(self):
        from quepasa.posts import is_junk
        assert not is_junk([])
