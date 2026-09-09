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
    ("/fix", "заменить текст вышедшего поста: /fix 445 Новый заголовок"),
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
        # отклонения воротами больше не приходят в чат по штуке; здесь
        # видно, не режет ли правило слишком много
        skipped = conn.execute(
            "SELECT count(*) FROM posts WHERE status = 'skipped' "
            "AND created_at >= now() - make_interval(hours => 24)"
        ).fetchone()["count"]

    return (
        f"<b>За сутки</b>\n\n"
        f"Статей: {row['статей']}, сюжетов: {row['сюжетов']}\n"
        f"Одиночек: {by_owner['одиночек']}\n"
        f"От двух владельцев: {by_owner['от2']}\n"
        f"От трёх: {by_owner['от3']}\n"
        f"От пяти: {by_owner['от5']}\n\n"
        f"Вышло постов: {posted}, отсеяно: {skipped}"
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
            f"{rows}\n\n<i>Поправить: /fix &lt;номер&gt; &lt;новый текст&gt;</i>")


def _fix(arg: str = "") -> str:
    """Заменяет шапку вышедшего поста: /fix 445 Новый заголовок.

    Удалить пост владелец может и сам в канале, а отредактировать
    сообщение бота там нельзя — это умеет только сам бот. Поэтому
    из всего, что делало уведомление о каждом посте, осталась
    единственная нужная часть, и та по запросу.
    """
    from .posts import rewrite_published

    parts = arg.strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[0].isdigit():
        return ("<b>/fix</b> — заменить текст вышедшего поста\n\n"
                "<code>/fix 445 Новый заголовок\nи лид со второй строки</code>\n\n"
                "<i>Номер поста виден в его ссылке. Текст идёт как есть, "
                "без правил имён и без ворот.</i>")
    return rewrite_published(int(parts[0]), parts[1])


def _queue() -> str:
    """Показывает саму очередь, а не её длину.

    По числу решение не примешь: чтобы завести имя или отклонить его, надо
    это имя увидеть. Поэтому здесь список, а следом — те же предложения
    с кнопками, что приходят сами: действовать надо на них.
    """
    from .db import connect
    from .entities import notify_new_unresolved
    from .telegram import notify_owner

    with connect() as conn:
        names = conn.execute(
            """
            SELECT surface_raw, surface, count FROM entity_unresolved
            WHERE ignored_at IS NULL ORDER BY count DESC, last_seen DESC LIMIT 20
            """
        ).fetchall()

    lines = ["<b>Ждёт решения</b>"]
    if names:
        lines += ["", f"<b>Имена ({len(names)})</b>"]
        for r in names:
            name = r["surface_raw"] or r["surface"]
            lines.append(f"• {html.escape(name)} ×{r['count']}")
    if not names:
        return "<b>Ждёт решения</b>\n\nОчередь пуста."

    # Сводку отправляем сами, до предложений: иначе список придёт после них
    # и читать его будет уже поздно.
    if names:
        lines += ["", "<i>Ниже — то же самое кнопками, действовать на них.</i>"]
    notify_owner("\n".join(lines))

    with connect() as conn:
        if names:
            conn.execute(
                "UPDATE entity_unresolved SET notified_at = NULL "
                "WHERE ignored_at IS NULL"
            )
            notify_new_unresolved(conn)
    return ""  # уже ответили сами


COMMANDS = {
    "/help": _help, "/start": _help,
    "/status": _status,
    "/stats": _stats,
    "/sync": _sync,
    "/refresh": _refresh,
    "/words": _words,
    "/queue": _queue,
    "/fix": _fix,
}


def run_command(text: str) -> str:
    """Выполняет команду и возвращает текст ответа.

    Пустая строка означает «команда ответила сама»: так делают те, кому
    нужен свой порядок сообщений или свои кнопки.
    """
    head, _, arg = text.strip().partition(" ")
    name = head.split("@")[0].lower()
    fn = COMMANDS.get(name)
    if fn is None:
        return f"Не знаю команды {html.escape(name)}.\n\n{_help()}"
    try:
        return fn(arg) if name == "/fix" else fn()
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
