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

    def test_filler_verb_with_subject_in_front(self):
        """«Решение отражает позицию» — тот же пустой оборот с подлежащим."""
        from quepasa.posts import clean_significance
        assert clean_significance("Решение отражает позицию правительства.") == ""

    def test_anchored_verbs_keep_informative_sentences(self):
        """«Подтверждает» в середине может нести факт — его не режем."""
        from quepasa.posts import clean_significance
        text = "Правительство подтверждает выплаты до декабря."
        assert clean_significance(text) == text


class TestNameFixes:
    """Испанские названия остаются испанскими.

    Живой случай: «Автономные сообщества ПП отказались обсуждать размещение
    несовершеннолетних Сеуты». Промпт запрещал перевод — модель обошла его
    транслитерацией аббревиатуры.
    """

    def test_cyrillic_abbreviation(self):
        from quepasa.posts import fix_names
        assert fix_names("Автономные сообщества ПП отказались") == \
            "Автономные сообщества PP отказались"

    def test_translated_party_name(self):
        from quepasa.posts import fix_names
        assert fix_names("Народная партия внесла поправку") == "PP внесла поправку"

    def test_declined_party_name(self):
        from quepasa.posts import fix_names
        assert fix_names("Заявление Народной партии") == "Заявление PP"

    def test_psoe_full_name(self):
        from quepasa.posts import fix_names
        assert fix_names("Испанская социалистическая рабочая партия выдвинула") == \
            "PSOE выдвинула"

    def test_transliterated_names(self):
        from quepasa.posts import fix_names
        assert fix_names("Подемос и Сумар") == "Podemos и Sumar"
        assert fix_names("Вокс поддержал") == "Vox поддержал"

    def test_latin_spelling_untouched(self):
        from quepasa.posts import fix_names
        text = "PP и PSOE договорились, Vox против"
        assert fix_names(text) == text

    def test_ordinary_russian_untouched(self):
        from quepasa.posts import fix_names
        text = "Правительство поддержало предложение о реформе"
        assert fix_names(text) == text

    def test_no_match_inside_words(self):
        """Границы слова: «Народность» — не партия."""
        from quepasa.posts import fix_names
        assert fix_names("Народность региона") == "Народность региона"


class TestRestoreLatinNames:
    """Транскрибированное имя возвращается к латинице по заголовкам источников.

    Правило «никакой транскрипции» есть в промпте, и всё равно в канал ушли
    «Лейре Диез», «Кристиану Роналду» и «Араухо». Латиница у нас на руках —
    в заголовках, откуда сюжет и собран.
    """

    def test_full_name(self):
        from quepasa.posts import restore_latin_names
        out = restore_latin_names(
            "Судья передал дело о Лейре Диез в Национальный суд",
            ["El primer juez del caso Leire Díez entrega la investigación"])
        assert "Leire Díez" in out and "Лейре" not in out

    def test_surname_only(self):
        """В заголовке часто остаётся одна фамилия."""
        from quepasa.posts import restore_latin_names
        out = restore_latin_names("Защитник Араухо покинул клуб",
                                  ["Ronald Araújo deja el Barcelona"])
        assert "Araújo" in out

    def test_different_transcription_still_matches(self):
        """«Роналду» и «Роналдо» — одна и та же ошибка."""
        from quepasa.posts import restore_latin_names
        titles = ["Cristiano Ronaldo se casa con Georgina Rodríguez"]
        assert "Cristiano Ronaldo" in restore_latin_names("Кристиану Роналду женился", titles)
        assert "Cristiano Ronaldo" in restore_latin_names("Кристиано Роналдо женился", titles)

    def test_geography_stays_russian(self):
        """Устоявшиеся русские названия трогать нельзя."""
        from quepasa.posts import restore_latin_names
        for text, titles in [
            ("Судья Мадрида принял решение", ["El juez de Madrid decide"]),
            ("Пожар в Валенсии потушен", ["Incendio en Valencia controlado"]),
            ("Правительство Испании одобрило", ["El Gobierno de España aprueba"]),
        ]:
            assert restore_latin_names(text, titles) == text

    def test_already_latin_untouched(self):
        from quepasa.posts import restore_latin_names
        text = "Pedro Sánchez выступил в Congreso"
        assert restore_latin_names(text, ["Pedro Sánchez comparece"]) == text

    def test_ordinary_russian_untouched(self):
        from quepasa.posts import restore_latin_names
        text = "Судья передал дело в Национальный суд"
        assert restore_latin_names(text, ["El juez entrega el caso"]) == text

    def test_no_titles_no_change(self):
        from quepasa.posts import restore_latin_names
        assert restore_latin_names("Лейре Диез", []) == "Лейре Диез"

    def test_empty_text(self):
        from quepasa.posts import restore_latin_names
        assert restore_latin_names("", ["Pedro Sánchez"]) == ""

    def test_short_surname_not_guessed(self):
        """«Ruiz» слишком короткое: совпадёт с чем угодно."""
        from quepasa.posts import restore_latin_names
        text = "Руис забил гол"
        assert restore_latin_names(text, ["Juan Ruiz marca un gol"]) == text

    def test_place_via_multiword_candidate(self):
        """«Sierra de Madrid» давала кандидата «Madrid» в обход списка мест
        и переписывала «в горах Мадрида»."""
        from quepasa.posts import restore_latin_names
        text = "В горах Мадрида потеряли ориентир"
        assert restore_latin_names(
            text, ["Desorientados en la Sierra de Madrid"]) == text

    def test_cities_stay_russian(self):
        from quepasa.posts import restore_latin_names
        for text, titles in [
            ("Матч в Барселоне", ["Partido en Barcelona"]),
            ("Пожар в Валенсии", ["Incendio en Valencia"]),
            ("Суд в Севилье", ["Juicio en Sevilla"]),
        ]:
            assert restore_latin_names(text, titles) == text


class TestTelegramRateLimit:
    """Массовая правка упирается в лимит канала — это штатный ответ, не сбой."""

    @staticmethod
    def _fake_post(responses):
        calls = {"n": 0}

        def post(url, json=None, timeout=None):
            r = responses[min(calls["n"], len(responses) - 1)]
            calls["n"] += 1
            return type("R", (), {"json": lambda s: r, "content": b"x", "text": ""})()

        return post, calls

    def test_waits_and_retries(self, monkeypatch):
        import quepasa.telegram as tg
        post, calls = self._fake_post([
            {"ok": False, "description": "Too Many Requests: retry after 2",
             "parameters": {"retry_after": 2}},
            {"ok": True, "result": {"message_id": 5}},
        ])
        monkeypatch.setattr(tg.httpx, "post", post)
        monkeypatch.setattr(tg.time, "sleep", lambda s: None)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
        assert tg._call("editMessageText", {})["message_id"] == 5
        assert calls["n"] == 2, "должна быть повторная попытка"

    def test_long_wait_is_not_honoured(self, monkeypatch):
        """Ждать десять минут хуже, чем оставить пост неправленым."""
        import pytest

        import quepasa.telegram as tg
        post, _ = self._fake_post([
            {"ok": False, "description": "Too Many Requests: retry after 600",
             "parameters": {"retry_after": 600}}])
        monkeypatch.setattr(tg.httpx, "post", post)
        monkeypatch.setattr(tg.time, "sleep", lambda s: None)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
        with pytest.raises(tg.TelegramError):
            tg._call("editMessageText", {})

    def test_other_errors_are_not_retried(self, monkeypatch):
        import pytest

        import quepasa.telegram as tg
        post, calls = self._fake_post([
            {"ok": False, "description": "Bad Request: message is not modified"}])
        monkeypatch.setattr(tg.httpx, "post", post)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
        with pytest.raises(tg.TelegramError):
            tg._call("editMessageText", {})
        assert calls["n"] == 1, "повтор осмыслен только для лимита"
