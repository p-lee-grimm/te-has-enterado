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

    def test_news_button_present_when_wiki_failed(self, monkeypatch):
        data = self._markup(monkeypatch, ["в Википедии не нашлось статьи"])
        assert "card:news:javier-negre" in data

    def test_no_one_tap_approve_when_unverified(self, monkeypatch):
        """Утвердить непроверенное одним нажатием нельзя — это и есть защита."""
        data = self._markup(monkeypatch, ["статья найдена поиском"])
        assert "card:ok:javier-negre" not in data

    def test_clean_draft_can_be_approved(self, monkeypatch):
        data = self._markup(monkeypatch, [])
        assert "card:ok:javier-negre" in data
        assert "card:news:javier-negre" in data


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
