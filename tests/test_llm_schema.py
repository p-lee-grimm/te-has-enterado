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


class TestClaudeCliProvider:
    """Провайдер через `claude -p`: разбор ответа CLI и понятные ошибки."""

    def _fake_run(self, payload, returncode=0, stderr=""):
        import json as _json
        import subprocess
        from types import SimpleNamespace

        stdout = payload if isinstance(payload, str) else _json.dumps(payload)

        def run(cmd, **kwargs):
            self.cmd = cmd
            return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)

        return run, subprocess

    def test_parses_result_and_usage(self, monkeypatch):
        import subprocess

        from quepasa.llm import LLMUsage, call_claude_cli

        run, _ = self._fake_run({
            "is_error": False, "subtype": "success",
            "result": '{"headline":"Заголовок"}',
            "usage": {"input_tokens": 100, "output_tokens": 20},
            "total_cost_usd": 0.0042,
        })
        monkeypatch.setattr(subprocess, "run", run)
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/claude")

        usage = LLMUsage()
        out = call_claude_cli("system", "user", usage)
        assert out == '{"headline":"Заголовок"}'
        assert usage.tokens_in == 100 and usage.tokens_out == 20
        # стоимость берём сообщённую CLI, а не считаем по прайсу
        assert usage.cost_usd == 0.0042

    def test_passes_prompts_as_flags(self, monkeypatch):
        import subprocess

        from quepasa.llm import LLMUsage, call_claude_cli

        run, _ = self._fake_run({"is_error": False, "result": "ok"})
        monkeypatch.setattr(subprocess, "run", run)
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/claude")

        call_claude_cli("СИСТЕМНЫЙ", "ПОЛЬЗОВАТЕЛЬСКИЙ", LLMUsage())
        assert "-p" in self.cmd
        assert self.cmd[self.cmd.index("-p") + 1] == "ПОЛЬЗОВАТЕЛЬСКИЙ"
        assert self.cmd[self.cmd.index("--system-prompt") + 1] == "СИСТЕМНЫЙ"
        # инструменты для генерации текста не нужны
        assert self.cmd[self.cmd.index("--allowedTools") + 1] == ""

    def test_auth_error_explains_what_to_do(self, monkeypatch):
        import subprocess

        from quepasa.llm import LLMError, LLMUsage, call_claude_cli

        run, _ = self._fake_run({
            "is_error": True,
            "result": "Failed to authenticate: OAuth session expired",
        })
        monkeypatch.setattr(subprocess, "run", run)
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/claude")

        with pytest.raises(LLMError) as ex:
            call_claude_cli("s", "u", LLMUsage())
        assert "авторизуйся" in str(ex.value)

    def test_non_json_output(self, monkeypatch):
        import subprocess

        from quepasa.llm import LLMError, LLMUsage, call_claude_cli

        run, _ = self._fake_run("это не json")
        monkeypatch.setattr(subprocess, "run", run)
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/claude")

        with pytest.raises(LLMError, match="не JSON"):
            call_claude_cli("s", "u", LLMUsage())

    def test_missing_binary(self, monkeypatch):
        from quepasa.llm import LLMError, LLMUsage, call_claude_cli

        monkeypatch.setattr("shutil.which", lambda b: None)
        with pytest.raises(LLMError, match="не найден в PATH"):
            call_claude_cli("s", "u", LLMUsage())

    def test_timeout(self, monkeypatch):
        import subprocess

        from quepasa.llm import LLMError, LLMUsage, call_claude_cli

        def run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 180)

        monkeypatch.setattr(subprocess, "run", run)
        monkeypatch.setattr("shutil.which", lambda b: "/usr/bin/claude")
        with pytest.raises(LLMError, match="не ответил"):
            call_claude_cli("s", "u", LLMUsage())
