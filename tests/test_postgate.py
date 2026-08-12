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


class TestHeadlineSubstance:
    """Заголовок сообщает, ЧТО сказано, а не что кто-то высказался.

    Живой случай: «Vivas ответил на заявление министра Марокко о Сеуте» —
    без lead и без significance это пост, из которого читатель не узнаёт
    ничего. Суть при этом была прямо в источниках: «Ceuta es España».
    """

    @staticmethod
    def _ok(headline, summary=""):
        from quepasa.postgate import _check_headline_substance
        from quepasa.stages.gate import GateReport
        report = GateReport()
        _check_headline_substance(report, headline, summary)
        return report.passed

    def test_bare_speech_act_without_lead_is_blocked(self):
        assert not self._ok("Vivas ответил на заявление министра Марокко о Сеуте")

    def test_same_headline_passes_with_a_lead(self):
        """Суть может быть и в lead — читатель её всё равно получит."""
        assert self._ok(
            "Vivas ответил на заявление министра Марокко о Сеуте",
            "Он назвал слова министра безосновательными и заявил, что Сеута — Испания.",
        )

    def test_substance_in_the_headline_passes(self):
        assert self._ok("Vivas назвал заявление министра Марокко безосновательным")

    def test_quoted_substance_passes(self):
        assert self._ok('Vivas ответил министру Марокко: «Сеута — это Испания»')

    def test_chto_clause_passes(self):
        assert self._ok("Vivas ответил, что Сеута остаётся испанской")

    def test_ordinary_headline_untouched(self):
        assert self._ok("Балеарские острова запретили продажу энергетиков подросткам")

    def test_other_speech_verbs_caught(self):
        assert not self._ok("Правительство прокомментировало ситуацию с поездами")
        assert not self._ok("Мадрид отреагировал на решение суда")
