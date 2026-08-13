"""Разбор ответа владельца на карточку.

Сообщение ревью просит «подтверди личность и поправь текст реплаем», и на
ссылку в ответе обработчик отвечал тем, что записывал её в карточку и
утверждал. В канал ушёл бы блок с голым адресом Википедии вместо пояснения.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quepasa.cards import looks_like_url, wiki_url_in  # noqa: E402

PUENTE = "https://es.wikipedia.org/wiki/%C3%93scar_Puente"


class TestWikiUrlInReply:
    def test_plain_article_url(self):
        assert wiki_url_in(PUENTE) == PUENTE

    def test_browser_quote_anchor_stripped(self):
        """Копирование из браузера добавляет «#:~:text=…» с цитатой.

        Заголовок статьи мы берём из адреса, и с этим хвостом он превращается
        в несуществующий."""
        pasted = (PUENTE + "#:~:text=%C3%93scar%20Puente%20Santiago%20"
                  "(Valladolid%2C%2015,Valladolid%20desde%202015%20hasta%202023.")
        assert wiki_url_in(pasted) == PUENTE

    def test_url_among_words(self):
        assert wiki_url_in(f"вот он: {PUENTE} — тот самый") == PUENTE

    def test_russian_wikipedia_accepted(self):
        url = "https://ru.wikipedia.org/wiki/Пуэнте"
        assert wiki_url_in(url) == url

    def test_mobile_domain_accepted(self):
        url = "https://es.m.wikipedia.org/wiki/Felipe_VI"
        assert wiki_url_in(url) == url

    def test_card_text_is_not_a_url(self):
        assert wiki_url_in("Министр транспорта Испании, бывший мэр Вальядолида.") is None

    def test_other_site_is_not_wikipedia(self):
        assert wiki_url_in("https://elpais.com/espana/puente.html") is None


class TestLooksLikeUrl:
    def test_bare_url(self):
        assert looks_like_url("https://elpais.com/x")

    def test_url_with_spaces_around(self):
        assert looks_like_url("  https://elpais.com/x  ")

    def test_prose_is_not_a_url(self):
        assert not looks_like_url("Министр транспорта Испании.")

    def test_prose_mentioning_url_is_not_bare(self):
        """Текст со ссылкой внутри — это всё-таки текст, а не голый адрес."""
        assert not looks_like_url("см. https://elpais.com/x подробнее")


class TestTitleFromUrl:
    """Заголовок из адреса: ссылку владелец копирует из браузера."""

    def test_encoded_title_decoded_once(self, monkeypatch):
        """Двойное кодирование давало 403 — и только на именах с диакритикой."""
        import quepasa.wiki as wiki
        seen = {}

        def fake_summary(title, lang):
            seen["title"] = title
            return {"extract": "текст", "url": "https://x", "title": title}

        monkeypatch.setattr(wiki, "summary", fake_summary)
        monkeypatch.setattr(wiki, "_with_retry", lambda fn: fn())
        wiki.fetch_for_entity("Óscar Puente", PUENTE)
        # подчёркивание — родная форма заголовка в Википедии, его не трогаем;
        # важно, что диакритика раскодирована ровно один раз
        assert seen["title"] == "Óscar_Puente"

    def test_plain_ascii_title_untouched(self, monkeypatch):
        import quepasa.wiki as wiki
        seen = {}

        def fake_summary(title, lang):
            seen["title"] = title
            return {"extract": "текст", "url": "https://x", "title": title}

        monkeypatch.setattr(wiki, "summary", fake_summary)
        monkeypatch.setattr(wiki, "_with_retry", lambda fn: fn())
        wiki.fetch_for_entity("Felipe VI", "https://es.wikipedia.org/wiki/Felipe_VI")
        assert seen["title"] == "Felipe_VI"


class TestSearchHint:
    """Уточнение к имени должно сужать поиск, а не топить его."""

    def test_long_context_is_trimmed(self, monkeypatch):
        """Склеенные заголовки давали запрос в 300 символов и ответ 500."""
        import quepasa.wiki as wiki
        seen = []
        monkeypatch.setattr(wiki, "search", lambda q, **k: seen.append(q) or [])
        wiki.fetch_for_entity("Javier Negre", None, "заголовок " * 60)
        assert len(seen[0]) < 100, f"запрос длиной {len(seen[0])}"

    def test_falls_back_to_bare_name(self, monkeypatch):
        import quepasa.wiki as wiki
        seen = []
        monkeypatch.setattr(wiki, "search", lambda q, **k: seen.append(q) or [])
        wiki.fetch_for_entity("Javier Negre", None, "periodista")
        assert seen == ["Javier Negre periodista", "Javier Negre"]


class TestReviewButtons:
    """Кнопка сборки нужна именно тогда, когда Википедия не помогла."""

    @staticmethod
    def _markup(monkeypatch, problems):
        import quepasa.cards as cards
        import quepasa.telegram as tg
        got = {}
        monkeypatch.setattr(tg, "notify_owner",
                            lambda t, **k: got.update(markup=k.get("reply_markup")))
        monkeypatch.setattr(cards, "news_urls_for", lambda e: [])
        cards.send_for_review(
            {"id": "javier-negre", "name_es": "Javier Negre", "type": "person"},
            {"card": "Журналист.", "problems": problems, "wiki_url": None},
        )
        return [b["callback_data"]
                for row in got["markup"]["inline_keyboard"] for b in row]

    def test_generate_button_present_when_wiki_failed(self, monkeypatch):
        data = self._markup(monkeypatch, ["в Википедии не нашлось статьи"])
        assert "card:gen:javier-negre" in data

    def test_no_one_tap_approve_when_unverified(self, monkeypatch):
        """Утвердить непроверенное одним нажатием нельзя — это и есть защита."""
        data = self._markup(monkeypatch, ["статья найдена поиском"])
        assert "card:ok:javier-negre" not in data

    def test_clean_draft_can_be_approved(self, monkeypatch):
        data = self._markup(monkeypatch, [])
        assert "card:ok:javier-negre" in data
        assert "card:gen:javier-negre" in data


class TestOffsetAdvance:
    """Смещение двигается ДО обработки: иначе нажатие повторяется вечно."""

    def test_offset_saved_before_slow_work(self, monkeypatch):
        import quepasa.cards as cards
        saved, order = {}, []

        class Conn:
            def execute(self, sql, params=None):
                if "bot_state" in sql and sql.strip().upper().startswith("INSERT"):
                    order.append("offset")
                    saved["offset"] = params[1]
                return type("R", (), {"fetchone": lambda s: None})()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        import quepasa.db as db
        monkeypatch.setattr(db, "connect", lambda *a, **k: Conn())
        monkeypatch.setattr(cards, "_state", lambda conn, key: None)
        import quepasa.telegram as tg
        monkeypatch.setattr(tg, "get_updates",
                            lambda offset=None, timeout=0: [{"update_id": 700}])
        monkeypatch.setattr(tg, "answer_callback", lambda *a: None)
        monkeypatch.setattr(tg, "edit_reply_markup", lambda *a: None)
        monkeypatch.setattr(tg, "notify_owner", lambda *a, **k: order.append("work"))

        cards.process_callbacks()
        assert saved["offset"] == "701", "подтверждаем приём сразу после получения"
        assert order[0] == "offset", "иначе долгий прогон вернётся к тому же нажатию"


class TestCardFromModel:
    """Карточка по знаниям модели: владелец принял риск и правит сам."""

    @staticmethod
    def _gen(monkeypatch, answer, hint=""):
        import quepasa.cards as cards
        seen = {}

        def fake_llm(system, user, usage, retries=1):
            seen["system"], seen["user"] = system, user
            return {"card": answer}

        monkeypatch.setattr(cards, "_llm_json", fake_llm)
        monkeypatch.setattr(cards, "verify_against_source",
                            lambda *a: ["сверка не должна вызываться"])
        return cards.generate_from_knowledge("Javier Negre", hint), seen

    def test_card_from_model_is_marked(self, monkeypatch):
        draft, _ = self._gen(monkeypatch, "Журналист, основатель Estado de Alarma.")
        assert draft["from_model"] is True
        assert draft["problems"] == []

    def test_no_source_verification(self, monkeypatch):
        """Сверять не с чем: модель писала по памяти, а не по тексту."""
        draft, _ = self._gen(monkeypatch, "Журналист.")
        assert draft["problems"] == [], "сверка с источником здесь неприменима"

    def test_news_go_in_as_identification_only(self, monkeypatch):
        _, seen = self._gen(monkeypatch, "Журналист.", hint="[ABC] Negre y el Rey")
        assert "не как источник" in seen["user"]

    def test_works_without_any_news(self, monkeypatch):
        """Новостей может не остаться — карточка всё равно должна собраться."""
        draft, _ = self._gen(monkeypatch, "Журналист.")
        assert draft["card"] == "Журналист."

    def test_empty_answer_is_an_error_not_a_card(self, monkeypatch):
        import pytest

        from quepasa.cards import CardError
        with pytest.raises(CardError):
            self._gen(monkeypatch, "  ")

    def test_too_long_card_still_flagged(self, monkeypatch):
        """Форму проверяем всегда: предел длины — не про источник."""
        draft, _ = self._gen(monkeypatch, "Ж" * 400)
        assert draft["problems"], "перебор по длине должен ловиться"


class TestModelCardReview:
    @staticmethod
    def _sent(monkeypatch, draft):
        import quepasa.cards as cards
        import quepasa.telegram as tg
        got = {}
        monkeypatch.setattr(tg, "notify_owner",
                            lambda t, **k: got.update(text=t, markup=k.get("reply_markup")))
        monkeypatch.setattr(cards, "news_urls_for", lambda e: [])
        cards.send_for_review(
            {"id": "javier-negre", "name_es": "Javier Negre", "type": "person"}, draft)
        return got

    def test_marked_as_model_written(self, monkeypatch):
        got = self._sent(monkeypatch, {"card": "Журналист.", "problems": [],
                                       "wiki_url": None, "from_model": True})
        assert "модели" in got["text"]

    def test_can_be_approved_in_one_tap(self, monkeypatch):
        """Владелец сказал: правлю сам. Значит кнопка «Ок» должна быть."""
        got = self._sent(monkeypatch, {"card": "Журналист.", "problems": [],
                                       "wiki_url": None, "from_model": True})
        data = [b["callback_data"] for r in got["markup"]["inline_keyboard"] for b in r]
        assert "card:ok:javier-negre" in data


class TestAutoApprove:
    """Карточка со статьёй Википедии за спиной идёт в посты сразу."""

    @staticmethod
    def _try(monkeypatch, draft):
        import quepasa.cards as cards
        import quepasa.db as db
        ran = []

        class Conn:
            def execute(self, sql, params=None):
                ran.append((" ".join(sql.split()), params))
                return type("R", (), {"fetchone": lambda s: None})()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(db, "connect", lambda *a, **k: Conn())
        return cards.approve_if_from_wikipedia("x", draft), ran

    def test_wikipedia_backed_card_is_approved(self, monkeypatch):
        draft = {"card": "Министр.", "problems": [], "wiki_url": "https://es.wikipedia.org/wiki/X"}
        ok, ran = self._try(monkeypatch, draft)
        assert ok is True
        assert draft["auto_approved"] is True
        assert any("card_status='approved'" in s for s, _ in ran)

    def test_card_with_problems_still_waits(self, monkeypatch):
        """Не прошло проверку — решает человек."""
        draft = {"card": "Министр.", "problems": ["слишком длинно"],
                 "wiki_url": "https://es.wikipedia.org/wiki/X"}
        ok, ran = self._try(monkeypatch, draft)
        assert ok is False and not ran

    def test_model_written_card_still_waits(self, monkeypatch):
        """Без статьи проверять не с чем — нажатие остаётся."""
        draft = {"card": "Журналист.", "problems": [], "wiki_url": None,
                 "from_model": True}
        ok, _ = self._try(monkeypatch, draft)
        assert ok is False

    def test_search_resolved_is_a_warning_not_a_blocker(self, monkeypatch):
        """Однофамилец возможен, поэтому предупреждаем — но не держим."""
        draft = {"card": "Министр.", "problems": [],
                 "warnings": ["статья найдена поиском по имени"],
                 "wiki_url": "https://es.wikipedia.org/wiki/X"}
        ok, _ = self._try(monkeypatch, draft)
        assert ok is True


class TestAutoApprovedReview:
    @staticmethod
    def _sent(monkeypatch, draft):
        import quepasa.cards as cards
        import quepasa.telegram as tg
        got = {}
        monkeypatch.setattr(tg, "notify_owner",
                            lambda t, **k: got.update(text=t, markup=k.get("reply_markup")))
        monkeypatch.setattr(cards, "news_urls_for", lambda e: [])
        cards.send_for_review({"id": "x", "name_es": "X", "type": "person"}, draft)
        return got

    def test_says_it_is_already_live(self, monkeypatch):
        got = self._sent(monkeypatch, {"card": "Министр.", "problems": [],
                                       "wiki_url": "https://w/x", "auto_approved": True})
        assert "добавлена в посты" in got["text"]

    def test_offers_removal_not_approval(self, monkeypatch):
        """Утверждать нечего — карточка уже в постах."""
        got = self._sent(monkeypatch, {"card": "Министр.", "problems": [],
                                       "wiki_url": "https://w/x", "auto_approved": True})
        data = [b["callback_data"] for r in got["markup"]["inline_keyboard"] for b in r]
        assert "card:del:x" in data and "card:ok:x" not in data

    def test_search_warning_is_shown(self, monkeypatch):
        got = self._sent(monkeypatch, {
            "card": "Министр.", "problems": [], "auto_approved": True,
            "warnings": ["статья найдена поиском по имени"],
            "wiki_url": "https://w/x"})
        assert "поиском" in got["text"]
