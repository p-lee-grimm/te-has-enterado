"""Действия над вышедшим постом из служебного чата.

Канал становится интерфейсом: владелец пересылает пост или кидает ссылку
и выбирает, что с ним сделать. Читать логи и помнить номера сюжетов для
этого не нужно.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from quepasa.postmenu import ACTIONS, message_id_in, run_action  # noqa: E402


class TestRecognisePost:
    """Пост опознаётся и по пересылке, и по ссылке."""

    def test_forward_from_channel(self):
        """Берём message_id канала, а не номер копии в служебном чате."""
        msg = {"message_id": 900,
               "forward_origin": {"type": "channel", "message_id": 445}}
        assert message_id_in(msg) == 445

    def test_legacy_forward_field(self):
        assert message_id_in({"forward_from_message_id": 445}) == 445

    def test_public_link(self):
        assert message_id_in({"text": "https://t.me/tehasenterado/445"}) == 445

    def test_internal_link(self):
        """Приватная форма ссылки — та, что копируется из клиента."""
        assert message_id_in({"text": "https://t.me/c/4499235636/445"}) == 445

    def test_link_among_words(self):
        msg = {"text": "вот тут кривой перевод t.me/tehasenterado/445 посмотри"}
        assert message_id_in(msg) == 445

    def test_link_in_caption(self):
        assert message_id_in({"caption": "t.me/tehasenterado/12"}) == 12

    @pytest.mark.parametrize("text", [
        "привет", "/stats", "надо поправить пост про Ceuta", "",
        "https://elpais.com/espana/2026/09/09/algo.html",
    ])
    def test_ordinary_messages_are_not_posts(self, text):
        assert message_id_in({"text": text}) is None


class TestMenuShape:
    def test_covers_requested_actions(self):
        codes = {c for c, _ in ACTIONS}
        # то, что просил владелец: контекст, перевод, дубликат, связь
        assert {"ctx", "tr", "dup", "rel"} <= codes

    def test_no_plain_delete(self):
        """Удалить пост в канале — два тапа; пересылка ради этого бессмысленна."""
        assert "del" not in {c for c, _ in ACTIONS}

    def test_every_action_is_handled(self, monkeypatch):
        """Кнопка без обработчика — это тупик, который видно только в бою."""
        import quepasa.postmenu as pm
        for code, _ in ACTIONS:
            monkeypatch.setattr(pm, "add_context", lambda p: "ok")
            monkeypatch.setattr(pm, "retranslate", lambda p: "ok")
            monkeypatch.setattr(pm, "add_sources", lambda p: "ok")
            monkeypatch.setattr(pm, "related_candidates", lambda p: ("ok", None))
            monkeypatch.setattr(pm, "mark_duplicate", lambda p: "ok")
            res = pm.run_action(code, 1)
            assert res, f"действие {code} ничего не вернуло"
            assert "Не знаю действия" not in str(res)

    def test_unknown_action_reports_itself(self):
        assert "Не знаю действия" in run_action("нету", 1)


class TestRetranslateKeepsUpd:
    """UPD переживает перевод: он про факты, а не про формулировку."""

    def test_upd_lines_survive(self, monkeypatch):
        import quepasa.postmenu as pm
        import quepasa.posts as posts
        saved = {}

        head = ("**Старый заголовок**\n\nСтарый лид.\n\n"
                "_UPD (21:30, El País): погибших 111_")

        class Conn:
            def execute(self, sql, params=None):
                if sql.strip().startswith("UPDATE"):
                    saved["header"] = params[0]
                    return type("R", (), {"fetchone": lambda s: None})()
                return type("R", (), {"fetchone": lambda s: {
                    "id": 1, "cluster_id": 2, "message_id": 445,
                    "header_md": head}})()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        import quepasa.db as db
        monkeypatch.setattr(db, "connect", lambda *a, **k: Conn())
        monkeypatch.setattr(posts, "generate_header",
                            lambda cid: ("**Новый заголовок**\n\nНовый лид.",
                                         "политика", {}))
        monkeypatch.setattr(posts, "refresh_one", lambda pid: 1)

        out = pm.retranslate(1)
        assert "UPD (21:30, El País): погибших 111" in saved["header"]
        assert "Новый заголовок" in saved["header"]
        assert "Старый заголовок" not in saved["header"]
        assert "/fix 445" in out, "если стало хуже, нужен способ поправить руками"


class TestAddContext:
    """Кнопка нажата — значит пояснение нужно сейчас, а не когда-нибудь.

    Живой случай: пост 444 про Garamendi. Действие смотрело только на уже
    заведённые сущности, Garamendi среди них не было, и владелец получил
    «в шапке нет заведённых имён» — то есть отказ вместо работы.
    """

    HEAD = ("**Garamendi предупредил о влиянии миграционного кризиса "
            "на суверенитет и экономику Сеуты**")

    @staticmethod
    def _run(monkeypatch, head, *, entity=None, pool=True, edited=1):
        import quepasa.entities as ent
        import quepasa.factops as fo
        import quepasa.facts as facts
        import quepasa.postmenu as pm
        import quepasa.posts as posts
        adopted = []

        class Conn:
            def execute(self, sql, params=None):
                if "FROM posts WHERE id" in sql:
                    return type("R", (), {"fetchone": lambda s: {
                        "id": 1, "cluster_id": 2, "message_id": 444,
                        "header_md": head, "entity_context": {}}})()
                if "FROM entities WHERE id" in sql:
                    return type("R", (), {"fetchone": lambda s: entity})()
                return type("R", (), {"fetchone": lambda s: None})()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        import quepasa.db as db
        monkeypatch.setattr(db, "connect", lambda *a, **k: Conn())
        monkeypatch.setattr(ent, "adopt_name",
                            lambda c, n: (adopted.append(n) or "garamendi", True))
        monkeypatch.setattr(facts, "has_pool", lambda c, e: pool)
        monkeypatch.setattr(fo, "refresh_entity",
                            lambda e, **k: {"facts": [{"fact": "x"}]})
        monkeypatch.setattr(posts, "backfill_entity_context",
                            lambda e, dry_run=True: {"edited": edited})
        return pm.add_context(1), adopted

    def test_unregistered_name_is_adopted(self, monkeypatch):
        out, adopted = self._run(
            monkeypatch, self.HEAD,
            entity={"name_es": "Garamendi", "never_explain": False})
        assert adopted == ["Garamendi"], "имя из шапки должно заводиться"
        assert "Garamendi" in out
        assert "пояснение в посте" in out

    def test_pool_built_when_missing(self, monkeypatch):
        out, _ = self._run(monkeypatch, self.HEAD, pool=False,
                           entity={"name_es": "Garamendi", "never_explain": False})
        assert "Garamendi" in out

    def test_no_facts_says_so_plainly(self, monkeypatch):
        out, _ = self._run(monkeypatch, self.HEAD, edited=0,
                           entity={"name_es": "Garamendi", "never_explain": False})
        assert "не нашлось" in out
        assert "/fix" in out, "нужен способ дать текст руками"

    def test_never_explain_skipped(self, monkeypatch):
        out, _ = self._run(monkeypatch, "**Felipe VI открыл сессию**",
                           entity={"name_es": "Felipe VI", "never_explain": True})
        assert "заведомо знакомые" in out

    def test_cyrillic_name_points_at_translation(self, monkeypatch):
        """Имя кириллицей — это ошибка перевода, а не отсутствие сущности."""
        out, _ = self._run(monkeypatch, "**Гараменди предупредил о кризисе**")
        assert "Перевести заново" in out
