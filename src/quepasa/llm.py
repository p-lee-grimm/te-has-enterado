"""Вызов LLM и валидация ответа по схеме.

Провайдер за одной функцией: модель задаётся конфигом, промпты лежат в
prompts/*.md и в коде не хардкодятся (§1).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator

from .config import env, get_settings

log = logging.getLogger(__name__)

TOPICS = {
    "политика", "экономика", "общество", "регионы",
    "международное", "происшествия", "культура/спорт",
}


class SummaryOut(BaseModel):
    """Схема ответа пересказа (§3.8). Невалидный ответ -> ретрай -> выброс кластера."""

    headline: str = Field(min_length=3, max_length=200)
    summary: str = Field(min_length=20)
    context: str = ""
    framing: str = ""
    topic: str = ""
    confidence: str = "high"

    @field_validator("confidence")
    @classmethod
    def _conf(cls, v: str) -> str:
        v = (v or "high").strip().lower()
        return v if v in ("high", "low") else "high"

    @field_validator("topic")
    @classmethod
    def _topic(cls, v: str) -> str:
        v = (v or "").strip().lower()
        return v if v in TOPICS else "общество"


@dataclass
class LLMUsage:
    tokens_in: int = 0
    tokens_out: int = 0
    calls: int = 0
    # стоимость, которую сообщил сам провайдер (CLI её знает точно)
    reported_cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        if self.reported_cost_usd:
            return self.reported_cost_usd
        s = get_settings()
        return (
            self.tokens_in / 1_000_000 * float(s.require("summarize.price_per_mtok_in"))
            + self.tokens_out / 1_000_000 * float(s.require("summarize.price_per_mtok_out"))
        )


class LLMError(RuntimeError):
    pass


_JSON_RE = re.compile(r"\{.*\}", re.S)


def extract_json(text: str) -> dict[str, Any]:
    """Достаёт JSON даже если модель обернула его в ```json."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = _JSON_RE.search(text)
    if not match:
        raise LLMError("В ответе модели нет JSON-объекта")
    return json.loads(match.group(0))


def call_anthropic(system: str, user: str, usage: LLMUsage) -> str:
    s = get_settings()
    key = env("ANTHROPIC_API_KEY", required=True)
    base = env("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")

    resp = httpx.post(
        f"{base}/v1/messages",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": s.require("summarize.model"),
            "max_tokens": int(s.require("summarize.max_tokens")),
            "temperature": float(s.require("summarize.temperature")),
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=180,
    )
    if resp.status_code >= 400:
        raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    u = data.get("usage", {})
    usage.tokens_in += u.get("input_tokens", 0)
    usage.tokens_out += u.get("output_tokens", 0)
    usage.calls += 1

    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return "".join(parts)


def call_claude_cli(system: str, user: str, usage: LLMUsage) -> str:
    """Вызов через `claude -p` — на подписке, без отдельного API-ключа.

    Удобно локально: консоль запускается из терминала, где ты уже авторизован,
    и генерация заголовка ничего дополнительно не стоит.

    Для GitHub Actions так не выйдет: в раннере нет интерактивной авторизации.
    Там нужен либо ANTHROPIC_API_KEY, либо долгоживущий токен из
    `claude setup-token`, положенный в секрет CLAUDE_CODE_OAUTH_TOKEN.
    """
    import shutil
    import subprocess

    s = get_settings()
    binary = s.get_path("summarize.cli_binary", "claude")
    if shutil.which(binary) is None:
        raise LLMError(
            f"{binary} не найден в PATH. Поставь Claude Code или переключи "
            "summarize.provider на anthropic."
        )

    cmd = [
        binary, "-p", user,
        "--system-prompt", system,
        "--output-format", "json",
        "--model", str(s.require("summarize.model")),
        # чистая генерация текста: инструменты не нужны и только замедляют
        "--allowedTools", "",
    ]
    timeout = int(s.get_path("summarize.cli_timeout_seconds", 180))

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise LLMError(f"{binary} не ответил за {timeout} с") from exc

    if not proc.stdout.strip():
        raise LLMError(f"{binary} ничего не вернул: {proc.stderr.strip()[:300]}")

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise LLMError(f"{binary} вернул не JSON: {proc.stdout[:300]}") from exc

    if data.get("is_error") or data.get("subtype") not in (None, "success"):
        # самая частая причина — протухшая авторизация; говорим прямо, что делать
        detail = str(data.get("result") or data.get("error") or "неизвестная ошибка")
        hint = ""
        if "authenticate" in detail.lower() or "oauth" in detail.lower():
            hint = (
                " Запусти `claude` в терминале и авторизуйся, либо переключи "
                "summarize.provider на anthropic с ANTHROPIC_API_KEY."
            )
        raise LLMError(f"{binary}: {detail}.{hint}")

    u = data.get("usage") or {}
    usage.tokens_in += int(u.get("input_tokens", 0))
    usage.tokens_out += int(u.get("output_tokens", 0))
    usage.calls += 1
    # CLI сам считает стоимость по подписке — она точнее нашей прикидки по ценам
    if data.get("total_cost_usd"):
        usage.reported_cost_usd += float(data["total_cost_usd"])

    return data.get("result") or ""


_PROVIDERS = {
    "anthropic": call_anthropic,
    "claude_cli": call_claude_cli,
}


def summarize_call(system: str, user: str, usage: LLMUsage) -> SummaryOut:
    """Один вызов с одним ретраем при невалидном JSON (§3.8)."""
    s = get_settings()
    provider = s.require("summarize.provider")
    fn = _PROVIDERS.get(provider)
    if fn is None:
        raise LLMError(f"Неизвестный провайдер LLM: {provider}")

    retries = int(s.require("summarize.retries"))
    last: Exception | None = None

    for attempt in range(retries + 1):
        try:
            raw = fn(system, user, usage)
            return SummaryOut.model_validate(extract_json(raw))
        except (LLMError, ValidationError, json.JSONDecodeError) as exc:
            last = exc
            usage.errors.append(f"{type(exc).__name__}: {str(exc)[:160]}")
            log.warning("Пересказ не удался (попытка %s): %s", attempt + 1, str(exc)[:200])
            if attempt < retries:
                user += (
                    "\n\nПредыдущий ответ не прошёл проверку схемы. "
                    "Верни строго один JSON-объект с полями "
                    "headline, summary, context, framing, topic, confidence."
                )

    raise LLMError(f"Пересказ не удался после {retries + 1} попыток: {last}")
