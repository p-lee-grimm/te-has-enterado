"""Операции владельца над пулом фактов: перезаливка, аудит, правка, откат.

Разделение с `facts.py` намеренное: там логика проверки и сборки, ничего
не знающая ни про Telegram, ни про посты; здесь — действия, у которых есть
последствия снаружи.

Предварительного ревью нет. Вместо него — выборочный аудит задним числом:
раз в неделю несколько случайных фактов приходят владельцу вместе с цитатой
и ссылкой. Это не одобрение, а сверка факта с фрагментом, и она не требует
экспертизы в испанской политике. Плюс правка и отставка с автоматическим
откатом в посты последних суток: право на ошибку с быстрым откатом заменяет
предварительную цензуру.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any

from .config import get_settings
from .db import connect

log = logging.getLogger(__name__)

KIND_RU = {
    "role": "роль",
    "scale": "масштаб",
    "classification": "классификация",
    "evaluative": "оценка",
    "legal": "процессуальный статус",
}

_FACT_ID_RE = re.compile(r"факт\s*#(\d+)", re.I)


def fact_id_in(text: str) -> int | None:
    """Номер факта из сообщения, на которое ответили реплаем."""
    m = _FACT_ID_RE.search(text or "")
    return int(m.group(1)) if m else None


def _rollback(entity_id: str) -> int:
    """Разносит изменившийся пул по постам последних суток.

    Тем же механизмом, что и снятие пометки об односторонности: пост уже
    висит в канале, и оставить в нём снятый или исправленный факт нельзя.
    Дальше суток не идём — пост той давности уже никто не открывает, а
    каждая правка это вызов API.
    """
    from .posts import refresh_posts_with_entity

    hours = float(get_settings().get_path("facts.rollback_window_hours", 24))
    try:
        return refresh_posts_with_entity(entity_id, window_hours=hours,
                                         reassemble=True)
    except Exception as exc:  # noqa: BLE001 — правка пула уже состоялась
        log.warning("Откат в посты по %s не удался: %s", entity_id, exc)
        return 0


# ------------------------------------------------------------- перезаливка


def refresh_entity(entity_id: str, *, dry_run: bool = True,
                   announce: bool = False, with_press: bool = True) -> dict[str, Any]:
    """Пересобирает пул одной сущности по всей лестнице источников."""
    from .facts import refresh
    from .telegram import notify_owner

    with connect() as conn:
        row = conn.execute("SELECT * FROM entities WHERE id = %s",
                           (entity_id,)).fetchone()
    if row is None:
        if announce:
            notify_owner(f"Сущности <code>{entity_id}</code> уже нет.")
        return {"status": "skip", "reason": "нет такой сущности"}

    entity = dict(row)
    stats = refresh(entity, dry_run=dry_run, with_press=with_press)

    if not dry_run:
        edited = _rollback(entity_id)
        if edited:
            stats["posts_updated"] = edited

    if announce:
        notify_owner(_refresh_report(entity, stats))
    return stats


def _refresh_report(entity: dict[str, Any], stats: dict[str, Any]) -> str:
    esc = html.escape
    lines = [f"<b>{esc(entity['name_es'])}</b> · пул фактов"]
    if not stats.get("sources"):
        lines.append("<i>Источников не нашлось. В посте останется только роль "
                     "в тексте — это штатный исход, а не сбой.</i>")
        return "\n".join(lines)

    lines.append(f"<i>источников {stats['sources']}, принято {stats['kept']}, "
                 f"отклонено {stats['rejected']}, ${stats.get('cost_usd', 0)}</i>")
    for f in stats.get("facts", [])[:6]:
        lines.append(f"• {esc(f['fact'])} <i>({KIND_RU.get(f['kind'], f['kind'])})</i>")
    if not stats.get("facts"):
        lines.append("<i>Ни один факт не прошёл проверку. Сущность остаётся "
                     "без пула.</i>")
    return "\n".join(lines)


def refresh_stale(dry_run: bool = True, limit: int = 10) -> dict[str, Any]:
    """Перепроверяет пул по очереди приоритетов.

    Первыми идут сущности с протухшим процессуальным статусом: устаревшее
    «фигурирует в расследовании» про оправданного человека — худший тип
    ошибки в системе, и ждать своей очереди оно не должно.
    """
    from .facts import expire_legal, recheck_queue

    stats: dict[str, Any] = {"expired": 0, "refreshed": 0, "failed": 0,
                             "posts_updated": 0, "cost_usd": 0.0}

    with connect() as conn:
        touched = expire_legal(conn)
        stats["expired"] = len(touched)
        queue = recheck_queue(conn, limit)

    # Просроченный статус исчезает из постов немедленно, не дожидаясь
    # успешной перепроверки: молчание безопаснее устаревшего обвинения.
    for entity_id in touched:
        if dry_run:
            log.info("DRY-RUN: сняли бы просроченный статус у %s", entity_id)
            continue
        stats["posts_updated"] += _rollback(entity_id)

    for entity in queue:
        if dry_run:
            log.info("DRY-RUN: перепроверили бы пул %s", entity["id"])
            continue
        res = refresh_entity(entity["id"], dry_run=False)
        if res.get("kept"):
            stats["refreshed"] += 1
        else:
            stats["failed"] += 1
        stats["cost_usd"] += float(res.get("cost_usd", 0))

    log.info("Перепроверка пула: просрочено %s, обновлено %s, без фактов %s",
             stats["expired"], stats["refreshed"], stats["failed"])
    return stats


# ------------------------------------------------------------------ аудит


def audit(dry_run: bool = True, days: int = 7) -> dict[str, Any]:
    """Случайные факты недели — владельцу, вместе с цитатой и ссылкой.

    Владелец сверяет факт с фрагментом источника. Это единственная проверка,
    которую человек без испанского политического бэкграунда действительно
    может выполнить, — и потому единственная, которую имеет смысл просить.
    """
    from .telegram import notify_owner

    n = int(get_settings().get_path("facts.audit_sample", 5))
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT f.*, e.name_es
            FROM entity_facts f JOIN entities e ON e.id = f.entity_id
            WHERE f.status = 'active'
              AND f.created_at >= now() - make_interval(days => %s)
            ORDER BY random() LIMIT %s
            """,
            (days, n),
        ).fetchall()

    if not rows:
        log.info("Аудит: новых фактов за %s дней нет", days)
        return {"sent": 0}

    for r in rows:
        if dry_run:
            log.info("DRY-RUN: показали бы факт #%s — %s", r["id"], r["fact"])
            continue
        notify_owner(_audit_message(r), reply_markup={"inline_keyboard": [[
            {"text": "✔️ Похоже на правду", "callback_data": f"fact:ok:{r['id']}"},
            {"text": "🗑 Снять", "callback_data": f"fact:retire:{r['id']}"},
        ]]})

    return {"sent": 0 if dry_run else len(rows)}


def _audit_message(row: dict[str, Any]) -> str:
    esc = html.escape
    lines = [
        f"<b>Факт #{row['id']}</b> · {esc(row['name_es'])}",
        "",
        esc(row["fact"]),
        f"<i>{KIND_RU.get(row['kind'], row['kind'])}"
        + (f", атрибуция: {esc(row['attribution'])}" if row["attribution"] else "")
        + "</i>",
    ]
    if row["quote"]:
        lines += ["", "<b>Цитата из источника:</b>",
                  f"<blockquote>{esc(row['quote'][:500])}</blockquote>"]
    if row["source_url"]:
        lines.append(f'<a href="{esc(row["source_url"], quote=True)}">Источник</a> '
                     f'· {row["source_tier"]}')
    lines += ["", "<i>Сверь факт с цитатой. Если формулировка неточна — "
                  "ответь реплаем исправленным текстом.</i>"]
    return "\n".join(lines)


# ------------------------------------------------------ правка и отставка


def fix(fact_id: int, new_text: str) -> str:
    """Заменяет текст факта. Решение владельца, а не предложение.

    Проверки слоя А прогоняются и показываются, но не блокируют: владелец
    видит цитату рядом с фактом и отвечает за формулировку сам. Запрет
    здесь означал бы предварительную цензуру, от которой мы и ушли.
    """
    from .facts import validate_fact

    new_text = " ".join((new_text or "").split())
    if not new_text:
        return "Пустой текст — факт не заменён."

    with connect() as conn:
        row = conn.execute("SELECT * FROM entity_facts WHERE id = %s",
                           (fact_id,)).fetchone()
        if row is None:
            return f"Факта #{fact_id} уже нет."
        # цитата остаётся прежней: заменяется формулировка, а не источник
        problems = validate_fact({**dict(row), "fact": new_text},
                                 row["quote"], row["source_tier"])
        conn.execute(
            "UPDATE entity_facts SET fact = %s, verified_at = now() WHERE id = %s",
            (new_text, fact_id),
        )

    edited = _rollback(row["entity_id"])
    log.info("Факт %s заменён правкой владельца", fact_id)

    answer = f"Факт #{fact_id} заменён."
    if edited:
        answer += f" Обновлено постов: {edited}."
    if problems:
        answer += "\n\n<i>Проверки, которые он не проходит: " \
                  + html.escape("; ".join(problems[:3])) + \
                  ". Оставлено как есть — решаешь ты.</i>"
    return answer


def retire(fact_id: int) -> bool:
    """Убирает факт из пула навсегда.

    Не удаляем строку: переизвлечение из того же источника вернуло бы её
    обратно, и снятый факт всплыл бы через неделю сам собой.
    """
    with connect() as conn:
        row = conn.execute(
            "UPDATE entity_facts SET status = 'retired' WHERE id = %s "
            "AND status <> 'retired' RETURNING entity_id",
            (fact_id,),
        ).fetchone()
    if row is None:
        return False
    _rollback(row["entity_id"])
    log.info("Факт %s снят владельцем", fact_id)
    return True
