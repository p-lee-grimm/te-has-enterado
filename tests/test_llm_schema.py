"""Схема JSON от LLM (§8.6). Мусор от модели не должен доходить до выпуска."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from quepasa.llm import LLMError, SummaryOut, extract_json  # noqa: E402

VALID = {
    "headline": "Правительство подняло минимальную зарплату",
    "summary": "Совет министров утвердил повышение. Профсоюзы поддержали решение. "
               "Работодатели отказались подписывать соглашение.",
    "context": "SMI — нижний порог оплаты труда, пересматривается ежегодно.",
    "framing": "",
    "topic": "экономика",
    "confidence": "high",
}


class TestExtractJson:
    def test_plain(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced(self):
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
        assert extract_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_with_chatter_around(self):
        assert extract_json('Вот результат:\n{"a": 1}\nГотово.') == {"a": 1}

    def test_no_json_raises(self):
        with pytest.raises(LLMError):
            extract_json("модель решила поговорить и не вернула объект")


class TestSummaryOut:
    def test_valid(self):
        out = SummaryOut.model_validate(VALID)
        assert out.topic == "экономика"
        assert out.confidence == "high"

    def test_unknown_topic_falls_back(self):
        out = SummaryOut.model_validate({**VALID, "topic": "спорт-политика-борщ"})
        assert out.topic == "общество"

    def test_topic_case_insensitive(self):
        assert SummaryOut.model_validate({**VALID, "topic": "Политика"}).topic == "политика"

    def test_bad_confidence_falls_back(self):
        assert SummaryOut.model_validate({**VALID, "confidence": "maybe"}).confidence == "high"
        assert SummaryOut.model_validate({**VALID, "confidence": "LOW"}).confidence == "low"

    def test_missing_headline_rejected(self):
        bad = {k: v for k, v in VALID.items() if k != "headline"}
        with pytest.raises(ValidationError):
            SummaryOut.model_validate(bad)

    def test_empty_summary_rejected(self):
        with pytest.raises(ValidationError):
            SummaryOut.model_validate({**VALID, "summary": ""})

    def test_optional_fields_default_empty(self):
        out = SummaryOut.model_validate(
            {"headline": "Заголовок", "summary": "Достаточно длинный пересказ события."}
        )
        assert out.context == "" and out.framing == ""
