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
