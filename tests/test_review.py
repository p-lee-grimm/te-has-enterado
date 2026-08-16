"""Поиск статьи-источника и разбор нажатий в служебном чате.

Здесь остались две вещи, пережившие отмену предварительного ревью карточек:
как находится статья, из которой извлекаются факты, и как обрабатываются
нажатия. Само ревью упразднено — вместо кнопки «Ок» работает выборочный
аудит задним числом.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

PUENTE = "https://es.wikipedia.org/wiki/%C3%93scar_Puente"


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


class TestOrgSubstitution:
    """Нет статьи о персоне — носителем контекста становится организация.

    У половины министров и почти у всех журналистов своей статьи нет,
    а у ведомства или канала — есть, и человек в ней назван по имени.
    """

    @staticmethod
    def _pick(monkeypatch, pages):
        import quepasa.sourcing as sourcing
        import quepasa.wiki as wiki

        monkeypatch.setattr(wiki, "search", lambda q, lang="es", limit=5: [
            {"title": t, "description": "", "key": t} for t in pages
        ] if lang == "es" else [])
        monkeypatch.setattr(
            sourcing, "_summary",
            lambda title, lang: {"title": title, "extract": pages[title],
                                 "url": f"https://es.wikipedia.org/wiki/{title}"})
        return sourcing._org_article("Javier Negre")

    def test_org_article_naming_the_person_is_taken(self, monkeypatch):
        got = self._pick(monkeypatch, {"EDATV": "Canal fundado por Javier Negre."})
        assert got and got["title"] == "EDATV"

    def test_org_not_naming_the_person_is_rejected(self, monkeypatch):
        """Иначе поиск подставил бы первую попавшуюся организацию."""
        got = self._pick(monkeypatch, {"Televisión": "Canal de televisión español."})
        assert got is None


class TestOffsetAdvance:
    """Смещение двигается ДО обработки: иначе нажатие повторяется вечно."""

    def test_offset_saved_before_slow_work(self, monkeypatch):
        import quepasa.review as review
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
        monkeypatch.setattr(review, "_state", lambda conn, key: None)
        import quepasa.telegram as tg
        monkeypatch.setattr(tg, "get_updates",
                            lambda offset=None, timeout=0: [{"update_id": 700}])
        monkeypatch.setattr(tg, "answer_callback", lambda *a: None)
        monkeypatch.setattr(tg, "edit_reply_markup", lambda *a: None)
        monkeypatch.setattr(tg, "notify_owner", lambda *a, **k: order.append("work"))

        review.process_callbacks()
        assert saved["offset"] == "701", "подтверждаем приём сразу после получения"
        assert order[0] == "offset", "иначе долгий прогон вернётся к тому же нажатию"


class TestFactReply:
    """Правка факта реплаем: владелец видит его в аудите и присылает текст."""

    def test_fact_id_parsed_from_audit_message(self):
        from quepasa.factops import fact_id_in
        assert fact_id_in("<b>Факт #42</b> · Óscar Puente") == 42

    def test_plain_message_has_no_fact_id(self):
        from quepasa.factops import fact_id_in
        assert fact_id_in("Просто сообщение") is None
