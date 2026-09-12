"""Классификатор значимости и фильтр разделов.

В сеть и в БД не ходим: проверяется решение — публиковать, в дайджест
или не публиковать вовсе, — и отсечение светских разделов на этапе fetch.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quepasa.impact import (  # noqa: E402
    DIGEST, DROP, POST, cached_route, clean, is_fresh, route, user_message,
)
from quepasa.stages.ingest import excluded_section  # noqa: E402
from quepasa.textutil import entry_sections, url_sections  # noqa: E402

NOW = datetime.now(timezone.utc)


# ------------------------------------------------------------------ разделы


def test_url_sections_ignores_dates_and_slug():
    # дата разделом не считается, слово из слага дальше по пути — тоже
    assert url_sections("https://www.elpais.com/gente/2026/08/30/viajes-de-verano") == ["gente"]
    assert url_sections("https://www.abc.es/2026/08/30/moda-y-presupuesto") == []


def test_url_sections_keeps_the_second_segment():
    """Второй сегмент бывает и подразделом, и слагом — поэтому список
    исключаемых разделов закрытый, а сравнение точное: «subida-del-smi»
    в него не попадёт, а «gente» попадёт."""
    assert url_sections("https://www.eldiario.es/economia/subida-del-smi") == [
        "economia", "subida-del-smi",
    ]


def test_url_sections_depth_cuts_the_slug():
    # второй сегмент — ещё раздел, третий уже материал
    url = "https://www.abc.es/deportes/futbol/estadio-presupuesto.html"
    assert url_sections(url) == ["deportes", "futbol"]
    assert url_sections(url, depth=1) == ["deportes"]


def test_entry_sections_normalizes_rss_categories():
    assert entry_sections([{"term": "Gente y TV"}, {"term": "corazon"}]) == [
        "gente-y-tv", "corazon",
    ]


def test_excluded_section_matches_url_or_rss():
    excluded = {"gente", "corazon"}
    assert excluded_section(["gente", "2026"], excluded) == "gente"
    assert excluded_section(["deportes", "futbol"], excluded) is None


# ------------------------------------------------------------------ вердикт


def test_clean_falls_back_to_background_not_soft():
    # опечатка модели не должна молча выбрасывать новость
    assert clean({"impact": "важно"})["impact"] == "background"
    assert clean({"impact": "SOFT", "confidence": "LOW"}) == {
        "impact": "soft", "reason": "", "confidence": "low",
    }


def test_route_by_value():
    assert route({"impact": "consequential", "confidence": "high"}) == POST
    assert route({"impact": "background", "confidence": "high"}) == DIGEST
    assert route({"impact": "soft", "confidence": "high"}) == DROP


def test_low_confidence_goes_to_digest_not_to_post():
    """Пограничный случай стоит слот и доверие — пусть будет строкой."""
    assert route({"impact": "consequential", "confidence": "low"}) == DIGEST


def test_no_verdict_keeps_the_old_path():
    # классификатор не ответил — решают прежние правила, канал не останавливается
    assert route(None) == POST
    assert route({}) == POST


def test_cached_route_ignores_stale_verdict():
    fresh = {"impact": "soft", "impact_confidence": "high", "impact_at": NOW}
    stale = {"impact": "soft", "impact_confidence": "high",
             "impact_at": NOW - timedelta(days=3)}
    assert cached_route(fresh) == DROP
    assert cached_route(stale) is None
    assert cached_route({"impact": None, "impact_at": NOW}) is None


def test_is_fresh_handles_missing_timestamp():
    assert is_fresh(None) is False
    assert is_fresh(NOW) is True


def test_user_message_carries_sections_and_lead():
    msg = user_message({"headlines": "- [ABC] Titular", "sections": "deportes",
                        "lead": "El club vendido."})
    assert "Titular" in msg
    assert "deportes" in msg
    assert "El club vendido." in msg


# ------------------------------------------------------------------ калибровка


def _eval_module():
    """scripts/ не пакет — грузим оценщик по пути."""
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "eval_impact.py"
    spec = importlib.util.spec_from_file_location("eval_impact", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_eval_counts_expensive_and_cheap_errors_apart():
    ev = _eval_module()
    rows = [
        {"label": "keep", "impact": "consequential"},   # верно опубликовано
        {"label": "drop", "impact": "consequential"},   # мусор в постах — дорогая
        {"label": "keep", "impact": "background"},      # новость потеряна — дешёвая
        {"label": "drop", "impact": "soft"},            # верно отсечено
    ]
    res = ev.evaluate(rows)
    assert (res["tp"], res["fp"], res["fn"], res["tn"]) == (1, 1, 1, 1)
    assert res["precision"] == 0.5
    assert res["recall"] == 0.5


def test_eval_holdout_split_is_every_third_row(tmp_path):
    ev = _eval_module()
    import json as _json

    path = tmp_path / "labeled.jsonl"
    path.write_text(
        "\n".join(_json.dumps({"id": i, "label": "keep", "impact": "consequential"})
                  for i in range(9)) + "\n",
        encoding="utf-8",
    )
    assert [r["id"] for r in ev.load(path, holdout=True)] == [2, 5, 8]
    assert [r["id"] for r in ev.load(path, holdout=False)] == [0, 1, 3, 4, 6, 7]


def test_eval_ignores_unlabeled_rows(tmp_path):
    """Пустой label — это «не размечено», а не «drop»: иначе вся неразмеченная
    часть выборки молча превращается в мусор и портит точность."""
    ev = _eval_module()
    import json as _json

    path = tmp_path / "labeled.jsonl"
    path.write_text(
        _json.dumps({"id": 1, "label": "", "impact": "consequential"}) + "\n"
        + _json.dumps({"id": 2, "label": "keep", "impact": "consequential"}) + "\n",
        encoding="utf-8",
    )
    assert [r["id"] for r in ev.load(path)] == [2]
