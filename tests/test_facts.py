"""Пул фактов: слой А, вердикт по цитатам, сборка и отбор по теме.

Извлечение и сборку тестами не покрываем — это вызовы модели. Покрываем то,
что решает без модели: что можно утверждать, при каком источнике, что
считается подтверждённым и чего сборщику нельзя написать.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from quepasa.facts import (  # noqa: E402
    match_legal_term, quote_found, select_facts, validate_assembly,
    validate_fact, verify_quotes,
)


def fact(text, kind="role", *, topics=("политика",), attribution="",
         quote="es presidente del Grupo ACS desde hace anos"):
    return {"fact": text, "kind": kind, "topics": list(topics),
            "attribution": attribution, "quote": quote}


class TestKinds:
    """Каждый тип разрешает своё, и это проверяется до всякой модели."""

    def test_plain_role_passes(self):
        assert validate_fact(fact("Председатель Grupo ACS")) == []

    def test_scale_qualitative_passes(self):
        """Качественная оценка масштаба разрешена и предпочтительна."""
        assert validate_fact(
            fact("Одна из крупнейших строительных компаний Европы", "scale")) == []

    def test_classification_passes(self):
        assert validate_fact(fact("Правоцентристская партия", "classification")) == []

    def test_unknown_kind_rejected(self):
        assert any("kind" in p for p in validate_fact(fact("Что-то", "прочее")))

    def test_too_long_rejected(self):
        problems = validate_fact(fact("Ж" * 200))
        assert any("длина" in p for p in problems)

    def test_empty_quote_rejected(self):
        """Факт без цитаты непроверяем, а значит его нет."""
        problems = validate_fact({**fact("Председатель Grupo ACS"), "quote": ""})
        assert any("quote" in p for p in problems)

    def test_topics_required(self):
        """Без темы сборщику нечем выбирать под пост."""
        problems = validate_fact(fact("Председатель Grupo ACS", topics=()))
        assert any("topics" in p for p in problems)


class TestForbiddenEverywhere:
    """Рейтинги, суммы и оценки тухнут или звучат от имени канала."""

    @pytest.mark.parametrize("text", [
        "786-я компания в рейтинге Forbes",
        "Вторая по тиражу газета страны",
        "Топ-10 строительных компаний",
    ])
    def test_ranking_rejected(self, text):
        assert validate_fact(fact(text)) != []

    @pytest.mark.parametrize("text", [
        "Состояние оценивается в 3,4 млрд",
        "Капитализация компании — 15 млрд евро",
    ])
    def test_absolute_numbers_rejected(self, text):
        assert validate_fact(fact(text)) != []

    def test_evaluative_word_outside_evaluative_rejected(self):
        problems = validate_fact(fact("Влиятельный предприниматель"))
        assert any("оценочные слова" in p for p in problems)

    def test_same_word_allowed_inside_evaluative(self):
        """С именем того, кто оценку высказал, она проверяема."""
        assert validate_fact(fact(
            "El País называет его влиятельным предпринимателем",
            "evaluative", attribution="El País")) == []

    def test_verbatim_copy_of_source_rejected(self):
        """То же правило, что и для пересказа: источник не переписываем."""
        source = ("председатель совета директоров группы ACS и президент "
                  "футбольного клуба Реал Мадрид с две тысячи")
        problems = validate_fact(
            {**fact("Председатель совета директоров группы ACS и президент "
                    "футбольного клуба Реал Мадрид с две тысячи")},
            source)
        assert any("дословный" in p for p in problems)


class TestAttribution:
    """Укоренённость в источнике не равна нейтральности."""

    def test_evaluative_without_attribution_rejected(self):
        problems = validate_fact(fact("Ультраправый журналист", "evaluative"))
        assert any("attribution" in p for p in problems)

    def test_attribution_only_in_field_rejected(self):
        """В блоке контекста утверждение прозвучит от имени канала."""
        problems = validate_fact(fact("Ультраправый журналист", "evaluative",
                                      attribution="El País"))
        assert any("отсутствует в тексте факта" in p for p in problems)

    def test_attribution_inside_the_text_passes(self):
        assert validate_fact(fact(
            "El País описывает EDATV как часть ультраправой медиасреды",
            "evaluative", topics=("медиа",), attribution="El País")) == []

    def test_self_description_is_evaluative_with_own_name(self):
        assert validate_fact(fact(
            "EDATV называет себя независимым изданием",
            "evaluative", topics=("медиа",), attribution="EDATV")) == []

    @pytest.mark.parametrize("text", [
        "Критики считают его ультраправым",
        "Многие называют канал пропагандистским",
        "Эксперты отмечают его связи с Vox",
    ])
    def test_vague_attribution_rejected(self, text):
        """Способ произнести утверждение, не отвечая за него."""
        problems = validate_fact(fact(text, "evaluative", attribution="критики"))
        assert any("размытая атрибуция" in p for p in problems)


class TestTiers:
    """Права убывают вместе с уровнем источника."""

    def test_press_may_state_a_role(self):
        assert validate_fact(fact("Министр транспорта"), tier="press") == []

    def test_press_may_not_classify(self):
        """Классификация из газетной статьи — пересказ чужого пересказа."""
        problems = validate_fact(fact("Правоцентристская партия", "classification"),
                                 tier="press")
        assert any("не разрешён" in p for p in problems)

    def test_official_may_not_characterise(self):
        """Ни одна партия не назовёт себя ультраправой."""
        problems = validate_fact(
            fact("El Mundo называет партию правой", "evaluative",
                 attribution="El Mundo"), tier="official")
        assert any("не разрешён" in p for p in problems)

    def test_wikidata_may_not_state_legal_status(self):
        problems = validate_fact(
            fact("Фигурирует в расследовании по делу Koldo", "legal",
                 topics=("право",)), tier="wikidata")
        assert any("не разрешён" in p for p in problems)


class TestLegal:
    """Процессуальный статус берётся только из закрытого словаря."""

    def test_dictionary_formulation_passes(self):
        assert validate_fact(fact("Фигурирует в расследовании по делу Koldo",
                                  "legal", topics=("право",))) == []

    def test_free_formulation_rejected(self):
        problems = validate_fact(fact("Замешан в коррупционном скандале", "legal",
                                      topics=("право",)))
        assert problems

    def test_investigado_is_not_an_accusation(self):
        """Термин ввели в 2015 взамен imputado именно из-за стигмы."""
        problems = validate_fact(fact("Обвиняемый по делу Koldo", "legal",
                                      topics=("право",)))
        assert any("словар" in p for p in problems)

    def test_case_must_be_named(self):
        """«Проходит по делу» без указания какого бесполезно читателю."""
        problems = validate_fact(fact("Фигурирует в расследовании по делу", "legal",
                                      topics=("право",)))
        assert problems

    def test_appealed_sentence_is_not_a_conviction(self):
        """«Осуждён» без оговорки — фактическая ошибка, а не неточность."""
        no_firme = match_legal_term(
            "Осуждён по делу Gürtel, приговор не вступил в силу")
        assert no_firme["es"] == "condenado (no firme)"

    def test_outcome_wins_over_stage(self):
        """Если источник содержит исход, факт обязан содержать исход."""
        source = "El juez le declaro absuelto tras la vista"
        problems = validate_fact(
            fact("Фигурирует в расследовании по делу Koldo", "legal",
                 topics=("право",)), source)
        assert any("исход" in p for p in problems)

    def test_guilt_wording_outside_legal_rejected(self):
        problems = validate_fact(fact("Замешан в деле Koldo", "role"))
        assert any("причастности" in p for p in problems)


class TestQuoteVerdict:
    """Вердикт слоя Б считает скрипт, а не критик."""

    SOURCE = ("Florentino Pérez es presidente del Grupo ACS y presidente "
              "del Real Madrid Club de Fútbol desde el año 2000.")

    def test_verbatim_quote_found(self):
        assert quote_found("presidente del Grupo ACS", self.SOURCE)

    def test_typography_does_not_matter(self):
        """Разница в кавычках и диакритике — типографика, а не подделка."""
        assert quote_found("Presidente del grupo ACS,", self.SOURCE)

    def test_invented_quote_not_found(self):
        assert not quote_found("es el hombre más influyente de España", self.SOURCE)

    def test_too_short_quote_is_not_a_proof(self):
        """Пара общих слов найдётся в любом тексте."""
        assert not quote_found("presidente", self.SOURCE)

    def test_critic_cannot_stamp_its_own_pass(self):
        """Даже если критик уверенно подтвердил, решает поиск в тексте."""
        checked = verify_quotes([
            fact("Председатель Grupo ACS", quote="presidente del Grupo ACS"),
            fact("Самый влиятельный человек Испании",
                 quote="el hombre más influyente de España"),
        ], self.SOURCE)
        assert checked[0]["found"] is True
        assert checked[1]["found"] is False


class TestAssembly:
    """Сборщик выбирает и соединяет, но не изобретает."""

    POOL = [
        {"id": 1, "fact": "Председатель Grupo ACS", "kind": "role",
         "topics": ["экономика"], "attribution": ""},
        {"id": 2, "fact": "Grupo ACS — одна из крупнейших строительных компаний "
                          "Европы", "kind": "scale", "topics": ["экономика"],
         "attribution": ""},
        {"id": 3, "fact": "Президент футбольного клуба Реал Мадрид", "kind": "role",
         "topics": ["спорт"], "attribution": ""},
        {"id": 4, "fact": "El País описывает его как близкого к правым",
         "kind": "evaluative", "topics": ["политика"], "attribution": "El País"},
    ]

    def test_words_from_the_facts_pass(self):
        res = validate_assembly(
            "Председатель Grupo ACS, одной из крупнейших строительных компаний "
            "Европы.", self.POOL[:2])
        assert res["passed"], res["foreign"]

    def test_case_agreement_is_not_an_invention(self):
        """Согласование падежей — разрешённая операция."""
        res = validate_assembly("Председателя Grupo ACS", self.POOL[:1])
        assert res["passed"], res["foreign"]

    def test_added_claim_is_caught(self):
        """«Давний союзник правых» в фактах отсутствует."""
        res = validate_assembly(
            "Председатель Grupo ACS, давний союзник правых", self.POOL[:2])
        assert not res["passed"]
        assert any("союзн" in w for w in res["foreign"])

    def test_service_words_allowed(self):
        res = validate_assembly(
            "Председатель Grupo ACS, которая является одной из крупнейших "
            "строительных компаний Европы", self.POOL[:2])
        assert res["passed"], res["foreign"]

    def test_too_long_rejected(self):
        res = validate_assembly("Председатель " * 40, self.POOL[:1])
        assert not res["passed"]

    def test_empty_assembly_is_a_valid_result(self):
        """Подходящих фактов не нашлось — это норма, а не сбой."""
        assert validate_assembly("", self.POOL)["passed"]


class TestSelection:
    """Один пул, разные посты — разные факты."""

    POOL = TestAssembly.POOL

    def test_topic_wins(self):
        picked = select_facts(self.POOL, "спорт", limit=2)
        assert picked[0]["id"] == 3

    def test_role_before_scale_on_tie(self):
        picked = select_facts(self.POOL, "экономика", limit=2)
        assert [f["id"] for f in picked] == [1, 2]

    def test_evaluative_only_on_its_own_topic(self):
        """Характеристика в чужом сюжете — ярлык, приклеенный заодно."""
        picked = select_facts(self.POOL, "экономика", limit=4)
        assert 4 not in [f["id"] for f in picked]

    def test_evaluative_taken_on_its_topic(self):
        picked = select_facts(self.POOL, "политика", limit=2)
        assert 4 in [f["id"] for f in picked]

    def test_limit_respected(self):
        assert len(select_facts(self.POOL, "экономика", limit=2)) == 2

    def test_empty_pool_gives_nothing(self):
        assert select_facts([], "экономика") == []
