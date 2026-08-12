"""Утверждённая карточка догоняет уже вышедшие посты.

Карточка утверждается позже, чем выходит пост: имя попадает в очередь,
владелец разбирает её вечером, а пост с этим именем уже висит в канале без
пояснения. Со звёздочкой у имени — она ставится тем же compose_html, и
проверять надо именно её: карточка без метки в тексте незаметна.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quepasa.posts import compose_html  # noqa: E402

ART = [{"source_id": "abc", "source_name": "ABC", "lean": "right",
        "owner_group": "vocento", "url": "https://abc.es/1",
        "url_canonical": "https://abc.es/1", "title": "Puente y el Rey",
        "type": "newspaper"}]
CARD = {"id": "oscar-puente", "name_es": "Óscar Puente", "name_ru": "",
        "card": "Министр транспорта Испании.", "card_status": "approved",
        "never_explain": False}


class TestRenderedPost:
    """Что видит читатель после того, как карточку разнесли по постам."""

    def _html(self, cards):
        from quepasa.entities import render_cards_html
        return compose_html(
            "**Óscar Puente раскритиковал встречу короля с журналистом**",
            ART, "политика",
            cards_html=render_cards_html(cards), cards=cards,
        )

    def test_card_block_appears(self):
        assert "Министр транспорта Испании." in self._html([CARD])

    def test_asterisk_marks_the_name(self):
        """Без звёздочки свёрнутый блок легко пропустить."""
        assert "Óscar Puente*" in self._html([CARD])

    def test_without_card_no_asterisk_and_no_block(self):
        html = self._html([])
        assert "Óscar Puente*" not in html
        assert "blockquote" not in html

    def test_asterisk_does_not_break_bold_header(self):
        """Имя в конце жирного заголовка — худший случай для markdown."""
        from quepasa.entities import render_cards_html
        html = compose_html("**Решение принял Óscar Puente**", ART, "политика",
                            cards_html=render_cards_html([CARD]), cards=[CARD])
        assert html.count("<b>") == html.count("</b>")

    def test_related_block_survives_reedit(self):
        """Блок «Ранее по теме» не сохранялся и пропадал при каждой правке."""
        html = compose_html("**Заголовок**", ART, "политика",
                            related_md="Ранее по теме: [прошлый пост](https://t.me/x/1)")
        assert "Ранее по теме" in html


class TestBackfillSelection:
    """Кого править, а кого не трогать."""

    class Conn:
        def __init__(self, ent, posts):
            self.ent, self.posts, self.updates = ent, posts, []

        def execute(self, sql, params=None):
            flat = " ".join(sql.split())
            if flat.startswith("UPDATE"):
                self.updates.append((flat, params))
                return type("R", (), {"fetchone": lambda s: None})()
            if "FROM entities WHERE id = %s" in flat:
                ent = self.ent
                return type("R", (), {"fetchone": lambda s: ent})()
            if flat.startswith("SELECT * FROM posts"):
                posts = self.posts
                return type("R", (), {"fetchall": lambda s: posts})()
            return type("R", (), {"fetchall": lambda s: [], "fetchone": lambda s: None})()

        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _run(self, monkeypatch, ent, posts):
        import quepasa.posts as posts_mod
        conn = self.Conn(ent, posts)
        # connect импортируется в posts на уровне модуля — подменяем там
        monkeypatch.setattr(posts_mod, "connect", lambda *a, **k: conn)
        monkeypatch.setattr(posts_mod, "cluster_articles", lambda c, cid: ART)
        return posts_mod.backfill_entity_card("oscar-puente", dry_run=True), conn

    def _post(self, pid=1, entity_ids=None, header="**Óscar Puente и король**"):
        return {"id": pid, "cluster_id": 10, "message_id": 100 + pid,
                "header_md": header, "category": "политика",
                "one_sided": False, "significance": "", "related_md": "",
                "entity_ids": entity_ids or []}

    def test_unapproved_card_is_not_distributed(self, monkeypatch):
        """Черновик в канал не уходит — это и есть смысл утверждения."""
        ent = {**CARD, "card_status": "draft"}
        res, _ = self._run(monkeypatch, ent, [self._post()])
        assert res["status"] == "skip"
        assert res["edited"] == 0

    def test_empty_card_is_not_distributed(self, monkeypatch):
        ent = {**CARD, "card": "   "}
        res, _ = self._run(monkeypatch, ent, [self._post()])
        assert res["status"] == "skip"

    def test_approved_card_reaches_the_post(self, monkeypatch):
        res, _ = self._run(monkeypatch, CARD, [self._post()])
        assert res["edited"] == 1

    def test_post_with_two_cards_is_left_alone(self, monkeypatch):
        """Три карточки превращают пост в справочник."""
        res, _ = self._run(monkeypatch, CARD, [self._post(entity_ids=["a", "b"])])
        assert res["edited"] == 0 and res["skipped_full"] == 1

    def test_cap_reported_not_silent(self, monkeypatch, caplog):
        """Молчаливое усечение читается как «обошли всё»."""
        import logging
        posts = [self._post(pid=i) for i in range(25)]
        with caplog.at_level(logging.INFO):
            res, _ = self._run(monkeypatch, CARD, posts)
        assert res["checked"] == 25
        assert res["edited"] == 20, "потолок из конфига"
        assert any("правим первые" in r.getMessage() for r in caplog.records), \
            "о пропущенных надо сказать вслух"

    def test_post_without_the_name_is_not_touched(self, monkeypatch):
        """Отбор идёт тем же mark_entities, что ставит звёздочку."""
        res, _ = self._run(monkeypatch, CARD,
                           [self._post(header="**Погода в Мадриде**")])
        assert res["checked"] == 0 and res["edited"] == 0

    def test_post_already_showing_the_card_is_skipped(self, monkeypatch):
        res, _ = self._run(monkeypatch, CARD,
                           [self._post(entity_ids=["oscar-puente"])])
        assert res["checked"] == 0

    def test_entity_created_after_the_post_is_still_found(self, monkeypatch):
        """Главный случай: сущности не было, когда пост выходил."""
        res, _ = self._run(monkeypatch, CARD, [self._post()])
        assert res["edited"] == 1, "запись в entity_mentions тут отсутствует"

    def test_never_explain_entity_is_not_distributed(self, monkeypatch):
        """Имена, которые аудитория заведомо знает, объяснять не надо."""
        ent = {**CARD, "never_explain": True}
        res, _ = self._run(monkeypatch, ent, [self._post()])
        assert res["status"] == "skip" and res["edited"] == 0


class TestHashtags:
    """Гео-тег хранился и проверялся воротами, но в пост не попадал."""

    def test_topic_and_place_both_present(self):
        from quepasa.posts import hashtags
        assert hashtags("политика", "#Сеута") == "#политика #Сеута"

    def test_topic_first(self):
        """По теме подписываются, по месту ищут."""
        from quepasa.posts import hashtags
        assert hashtags("экономика", "#Каталония").startswith("#экономика")

    def test_national_news_has_no_place(self):
        """#Испания в канале про Испанию ничего не сужает."""
        from quepasa.posts import hashtags
        assert hashtags("политика", None) == "#политика"
        assert hashtags("политика", "") == "#политика"

    def test_geo_tag_reaches_the_post(self):
        html = compose_html("**Robles посетила Сеуту**", ART, "политика",
                            geo_tag="#Сеута")
        assert "#Сеута" in html

    def test_geo_tag_survives_reedit(self):
        """Как и related_md: при правке пересобирается весь текст."""
        from quepasa.entities import render_cards_html
        html = compose_html("**Заголовок**", ART, "политика",
                            cards_html=render_cards_html([CARD]), cards=[CARD],
                            geo_tag="#КастилияИЛеон")
        assert "#КастилияИЛеон" in html

    def test_compound_name_stays_one_tag(self):
        """Дефис и пробел обрывают хэштег — потому и camel case."""
        from quepasa.posts import hashtags
        assert hashtags("политика", "#КастилияЛаМанча") == "#политика #КастилияЛаМанча"


class TestSignificanceRendering:
    def test_empty_significance_adds_nothing(self):
        """Пустая строка — норма, а не недоработка."""
        html = compose_html("**Заголовок**", ART, "политика", significance="")
        assert "<i>" not in html

    def test_significance_is_italic_when_present(self):
        html = compose_html("**Заголовок**", ART, "политика",
                            significance="Заявление подают до 30 сентября.")
        assert "<i>Заявление подают до 30 сентября.</i>" in html


class TestCleanSignificance:
    """Промпт просит, валидатор гарантирует."""

    import pytest as _pytest

    @_pytest.mark.parametrize("junk", [
        "Касается жителей Сеуты, испытывающих опасения в связи с безопасностью.",
        "Касается болельщиков Barcelona и интересующихся испанским футболом.",
        "Касается отношений Испании и Марокко по вопросу Сеуты.",
        "Подтверждение позиции Испании по суверенитету над анклавами",
        "Отражает политический конфликт вокруг миграционной политики",
        "Случай привлёк внимание к вопросам защиты прав детей.",
        "Разворачивается полемика о том, как встречи влияют на имидж.",
        "Важно для тех, кто живёт в Каталонии.",
        "Может привести к обострению отношений.",
        "Показывает позицию правительства по вопросу.",
    ])
    def test_restatement_is_dropped(self, junk):
        from quepasa.posts import clean_significance
        assert clean_significance(junk) == ""

    @_pytest.mark.parametrize("good", [
        "Заявление на помощь подают до 30 сентября, через портал SEPE.",
        "Линия закрыта до декабря, поезда идут в объезд через Валенсию.",
        "Запрет касается розничной продажи энергетиков подросткам на архипелаге.",
        "Редкое явление совпадает с жарой: смотреть только через фильтр.",
        "Продлевать NIE по старым правилам можно ещё три месяца.",
    ])
    def test_practical_consequence_survives(self, good):
        from quepasa.posts import clean_significance
        assert clean_significance(good) == good

    def test_empty_stays_empty(self):
        from quepasa.posts import clean_significance
        assert clean_significance("") == "" and clean_significance(None) == ""

    def test_junk_word_inside_sentence_is_not_a_trigger(self):
        """Ловим начало строки: «касается» в середине — обычное слово."""
        from quepasa.posts import clean_significance
        text = "Запрет касается продажи энергетиков подросткам."
        assert clean_significance(text) == text
