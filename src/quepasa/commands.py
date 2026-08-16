"""Команды бота в служебном чате.

Владелец работает с телефона, консоли под рукой нет. Всё, что раньше
делалось через manage.py, должно вызываться сообщением в чат ревью —
иначе обслуживание требует ноутбука и не делается вовсе.

Команды возвращают текст ответа; отправляет его вызывающий. Долгих
операций здесь нет: то, что идёт минутами, отвечает «запустил» и
досылает итог само.
"""

from __future__ import annotations

import html
import logging

log = logging.getLogger(__name__)

HELP = [
    ("/status", "здоровье: сбор, фиды, публикация, очередь"),
    ("/stats", "сколько сюжетов и сколько источников за сутки"),
    ("/sync", "дополнить вышедшие посты новыми изданиями"),
    ("/refresh", "пересобрать посты по текущим правилам вёрстки"),
    ("/words", "найти выдуманные слова в вышедших постах"),
    ("/queue", "что ждёт решения"),
    ("/help", "этот список"),
]


def _help() -> str:
    rows = "\n".join(f"{c} — {d}" for c, d in HELP)
    return f"<b>Команды</b>\n\n{rows}"


def _status() -> str:
    from .status import checks, collect

    lines = [f"{'✅' if ok else '⚠️'} {name} — {detail}"
             for name, ok, detail in checks(collect())]
    return "<b>Состояние</b>\n\n" + "\n".join(lines)


def _stats() -> str:
    from .db import connect

    with connect() as conn:
        row = conn.execute(
            """
            SELECT count(*) AS статей, count(DISTINCT cluster_id) AS сюжетов
            FROM articles WHERE published_at >= now() - make_interval(hours => 24)
            """
        ).fetchone()
        by_owner = conn.execute(
            """
            WITH c AS (
                SELECT a.cluster_id, count(DISTINCT coalesce(s.owner_group, s.id)) AS n
                FROM articles a JOIN sources s ON s.id = a.source_id
                WHERE a.cluster_id IS NOT NULL
                  AND a.published_at >= now() - make_interval(hours => 24)
                GROUP BY a.cluster_id)
            SELECT count(*) FILTER (WHERE n = 1) AS одиночек,
                   count(*) FILTER (WHERE n >= 2) AS от2,
                   count(*) FILTER (WHERE n >= 3) AS от3,
                   count(*) FILTER (WHERE n >= 5) AS от5
            FROM c
            """
        ).fetchone()
        posted = conn.execute(
            "SELECT count(*) FROM posts WHERE status = 'published' "
            "AND published_at >= now() - make_interval(hours => 24)"
        ).fetchone()["count"]

    return (
        f"<b>За сутки</b>\n\n"
        f"Статей: {row['статей']}, сюжетов: {row['сюжетов']}\n"
        f"Одиночек: {by_owner['одиночек']}\n"
        f"От двух владельцев: {by_owner['от2']}\n"
        f"От трёх: {by_owner['от3']}\n"
        f"От пяти: {by_owner['от5']}\n\n"
        f"Вышло постов: {posted}"
    )


def _sync() -> str:
    from .posts import sync_all

    res = sync_all(dry_run=False)
    return (f"<b>Дополнение постов</b>\n\nПроверено: {res['checked']}, "
            f"дополнено: {res['edited']}, ошибок: {res['errors']}")


def _refresh() -> str:
    from .posts import refresh_published

    res = refresh_published(dry_run=False)
    return (f"<b>Пересборка</b>\n\nПроверено: {res['checked']}, "
            f"изменено: {res['edited']}, без изменений: {res['unchanged']}, "
            f"ошибок: {res['errors']}")


def _words() -> str:
    """Ищет в вышедших постах слова, которых нет в русском языке.

    Кодом этот класс не ловится: выдуманное слово от настоящего отличает
    словарь. Поэтому спрашиваем модель — одним вызовом на весь список
    редких слов, а не по слову.
    """
    from .lint import invented_words

    found = invented_words()
    if not found:
        return "<b>Проверка слов</b>\n\nВыдуманных слов не нашлось."
    rows = "\n".join(
        f"• <b>{html.escape(w)}</b> — пост {mid}" for w, mid in found)
    return ("<b>Проверка слов</b>\n\nПохоже на транскрипцию вместо перевода:\n"
            f"{rows}\n\n<i>Ответь реплаем на уведомление о посте, "
            "чтобы переписать шапку.</i>")


def _queue() -> str:
    from .db import connect

    with connect() as conn:
        ent = conn.execute(
            "SELECT count(*) FROM entity_unresolved WHERE ignored_at IS NULL"
        ).fetchone()["count"]
        edits = conn.execute(
            "SELECT count(*) FROM post_edits WHERE status = 'pending'"
        ).fetchone()["count"]
    return (f"<b>Ждёт решения</b>\n\nИмён в очереди: {ent}\n"
            f"Правок фактов: {edits}")


COMMANDS = {
    "/help": _help, "/start": _help,
    "/status": _status,
    "/stats": _stats,
    "/sync": _sync,
    "/refresh": _refresh,
    "/words": _words,
    "/queue": _queue,
}


def run_command(text: str) -> str:
    """Выполняет команду и возвращает текст ответа."""
    name = text.strip().split()[0].split("@")[0].lower()
    fn = COMMANDS.get(name)
    if fn is None:
        return f"Не знаю команды {html.escape(name)}.\n\n{_help()}"
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — ответ обязателен в любом случае
        log.exception("Команда %s не выполнилась", name)
        return f"{html.escape(name)} не выполнилась: {html.escape(str(exc)[:200])}"


def register() -> None:
    """Отдаёт список команд Telegram, чтобы работала подсказка при вводе."""
    from .telegram import TelegramError, _call, review_chat_id

    try:
        _call("setMyCommands", {
            "commands": [{"command": c.lstrip("/"), "description": d}
                         for c, d in HELP],
            "scope": {"type": "chat", "chat_id": review_chat_id()},
        })
    except TelegramError as exc:
        log.warning("setMyCommands: %s", exc)
