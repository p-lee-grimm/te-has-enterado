"""Критик поста: узнает ли читатель, что произошло.

Ворота в postgate проверяют это механически — по списку глаголов речи
(«ответил», «прокомментировал»). Список модель обходит формулировкой:
«Fiscal general провела грань между критикой правосудия и дискредитацией
Fiscalía» — глагола из списка нет, проверка молчит, а пост не сообщает
ничего. Так же до этого обходились словарь названий партий и запрет
на кальки; расширять список в четвёртый раз бессмысленно.

Поэтому здесь второе мнение модели о нашем же тексте — тот же приём, что
и fact_critic для фактов. Критик видит заголовки источников и наш пост и
отвечает на один вопрос. Если суть в источниках была, а в пост не попала,
он говорит, какой именно факт туда поставить, и заголовок переписывается
один раз с этой подсказкой.

Не прошедший и со второй попытки сюжет не выбрасывается: он уходит строкой
в «Коротко», где голый заголовок — штатный формат, а не недоработка.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import get_settings, load_prompt

log = logging.getLogger(__name__)


def enabled() -> bool:
    return bool(get_settings().get_path("critic.enabled", False))


def clean(data: dict[str, Any]) -> dict[str, Any]:
    """Ответ модели -> вердикт. Невнятный ответ считаем «прошло».

    Критик — страховка, а не ворота: если он сам сломался, пост выходит
    прежним путём. Обратное поведение остановило бы канал из-за вспомогательной
    проверки, а это дороже, чем один невнятный пост.
    """
    conveys = data.get("conveys")
    if not isinstance(conveys, bool):
        conveys = True
    return {
        "conveys": conveys,
        "missing": str(data.get("missing") or "").strip()[:200],
        "fix": str(data.get("fix") or "").strip()[:300],
    }


def user_message(headlines: list[str], header_md: str) -> str:
    limit = int(get_settings().get_path("critic.max_headlines", 8))
    lines = "\n".join(f"- {h}" for h in headlines[:limit])
    post = (header_md or "").replace("**", "").strip()
    return f"Заголовки источников:\n{lines}\n\nНаш пост:\n{post}"


def check(headlines: list[str], header_md: str, usage=None) -> dict[str, Any]:
    """Вердикт по готовому посту. При сбое — «прошло»: канал не останавливаем."""
    from .llm import LLMUsage, json_call

    usage = usage if usage is not None else LLMUsage()
    if not headlines or not (header_md or "").strip():
        return {"conveys": True, "missing": "", "fix": ""}

    try:
        data = json_call(load_prompt("post_critic.md"),
                         user_message(headlines, header_md), usage)
    except Exception as exc:  # noqa: BLE001 — критик не роняет публикацию
        log.warning("Критик не ответил: %s", str(exc)[:160])
        return {"conveys": True, "missing": "", "fix": ""}

    verdict = clean(data)
    if not verdict["conveys"]:
        log.info("Критик: пост не передаёт суть — %s (поставить: %s)",
                 verdict["missing"], verdict["fix"])
    return verdict


def hint_for_retry(verdict: dict[str, Any]) -> str:
    """Подсказка, с которой заголовок переписывается во второй раз."""
    parts = ["Предыдущий заголовок не сообщил читателю, что произошло."]
    if verdict.get("missing"):
        parts.append(f"Не хватает: {verdict['missing']}.")
    if verdict.get("fix"):
        parts.append(f"Поставь в заголовок сам факт: {verdict['fix']}.")
    parts.append("Заголовок должен называть событие, а не то, что кто-то высказался.")
    return " ".join(parts)
