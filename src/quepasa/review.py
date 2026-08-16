"""Служебный чат: разбор нажатий и правок владельца.

Предварительное подтверждение справок упразднено. Владелец не обладает
экспертизой в испанской политике и не может оценить содержание лучше модели —
кнопка «Ок» была ритуалом, создающим ложное ощущение контроля при сохранении
полной ответственности.

Вместо неё работают структурные ограничения на форму утверждений, обязательная
цитата под каждым фактом, кросс-спектральная проверка спорного, выборочный
аудит задним числом и быстрый откат. Здесь живёт последнее: кнопки под
черновиками постов и правками, всплывающие пояснения в замерном режиме
и правка факта реплаем прямо из чата.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def _state(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM bot_state WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else None


def _set_state(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO bot_state (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, value),
    )


def news_source_text(conn, name: str, limit: int = 8) -> str:
    """Заголовки и подводки новостей, где встретилось имя.

    Нужны как уточнитель при поиске статьи: у испанских имён сплошные
    совпадения, и «Galán» без контекста находит колумбийского политика.
    Источником фактов эти тексты не являются — для этого есть лестница
    источников с проверкой цитат.
    """
    import re

    # По границам слова, а не подстрокой: ILIKE '%INE%' находит «cine»
    # и «Medicine», и уточнение получает новости, к сущности не относящиеся.
    pattern = r"\y" + re.escape(name) + r"\y"
    rows = conn.execute(
        """
        SELECT a.title, coalesce(a.summary_feed, '') AS summary, s.name AS source
        FROM articles a JOIN sources s ON s.id = a.source_id
        WHERE a.title ~* %s OR a.summary_feed ~* %s OR a.body ~* %s
        ORDER BY a.published_at DESC NULLS LAST
        LIMIT %s
        """,
        (pattern, pattern, pattern, limit),
    ).fetchall()
    return "\n\n".join(
        f"[{r['source']}] {r['title']}\n{r['summary'][:400]}".strip() for r in rows
    )


def process_callbacks(timeout: int = 0) -> dict[str, int]:
    """Разбирает нажатия кнопок и правки фактов реплаем.

    Вызывается из обычного прогона: держать отдельный долгоживущий процесс
    ради нескольких кнопок в неделю — лишняя движущаяся часть.
    """
    from .db import connect
    from .telegram import (
        TelegramError, answer_callback, edit_reply_markup, get_updates, notify_owner,
    )

    stats: dict[str, int] = {"approved": 0, "deleted": 0, "edited": 0}
    with connect() as conn:
        offset = _state(conn, "updates_offset")

    try:
        updates = get_updates(offset=int(offset) if offset else None, timeout=timeout)
    except TelegramError as exc:
        log.warning("getUpdates: %s", exc)
        return stats

    # Приём подтверждаем СРАЗУ, до обработки. Иначе долгий или упавший прогон
    # вернётся к тому же нажатию в следующий раз — работа делается заново,
    # за модель платим снова, и так по кругу каждые две минуты. Потерять одно
    # нажатие при сбое дешевле, чем зациклить его навсегда.
    if updates:
        with connect() as conn:
            _set_state(conn, "updates_offset", str(updates[-1]["update_id"] + 1))

    unhandled = 0
    for upd in updates:
        cq = upd.get("callback_query")
        data = (cq.get("data") or "") if cq else ""

        if cq and data.startswith("edit:"):
            from .edits import apply_edit

            _, action, edit_id = data.split(":", 2)
            with connect() as conn:
                if action == "apply":
                    ok = apply_edit(conn, int(edit_id))
                    stats["edits_applied"] = stats.get("edits_applied", 0) + int(ok)
                else:
                    conn.execute(
                        "UPDATE post_edits SET status='skipped', decided_at=now() "
                        "WHERE id=%s", (int(edit_id),))
                    stats["edits_skipped"] = stats.get("edits_skipped", 0) + 1
            answer_callback(cq["id"], "Заменено" if action == "apply" else "Оставили")
            msg = cq.get("message") or {}
            if msg:
                edit_reply_markup(str(msg["chat"]["id"]), msg["message_id"])
            continue

        if cq and data.startswith("post:"):
            from .posts import publish

            _, action, cluster_id = data.split(":", 2)
            if action == "pub":
                try:
                    publish(int(cluster_id), dry_run=False)
                    stats["posts_published"] = stats.get("posts_published", 0) + 1
                    answer_callback(cq["id"], "Опубликовано")
                except Exception as exc:  # noqa: BLE001
                    log.warning("Публикация из ревью не удалась: %s", exc)
                    answer_callback(cq["id"], "Не получилось")
            else:
                with connect() as conn:
                    conn.execute(
                        "UPDATE posts SET status='skipped' WHERE cluster_id=%s "
                        "AND status='draft'", (int(cluster_id),))
                stats["posts_skipped"] = stats.get("posts_skipped", 0) + 1
                answer_callback(cq["id"], "Пропущено")
            msg = cq.get("message") or {}
            if msg:
                edit_reply_markup(str(msg["chat"]["id"]), msg["message_id"])
            continue

        if cq and data.startswith("e:"):
            from .entities import handle_tap

            _, entity_id, post_id = data.split(":", 2)
            with connect() as conn:
                text = handle_tap(conn, entity_id, int(post_id or 0),
                                  (cq.get("from") or {}).get("id"))
            # show_alert: пояснение показывается окном, а не строкой сверху
            try:
                from .telegram import _call
                _call("answerCallbackQuery", {"callback_query_id": cq["id"],
                                              "text": text, "show_alert": True})
            except Exception as exc:  # noqa: BLE001
                log.warning("answerCallbackQuery: %s", exc)
            stats["taps"] = stats.get("taps", 0) + 1
            continue

        # факт из выборочного аудита: снять или оставить
        if cq and data.startswith("fact:"):
            from .factops import retire

            _, action, fact_id = data.split(":", 2)
            if action == "retire":
                res = retire(int(fact_id))
                stats["retired"] = stats.get("retired", 0) + 1
                answer_callback(cq["id"], "Снят" if res else "Уже снят")
            else:
                answer_callback(cq["id"], "Оставили")
            msg = cq.get("message") or {}
            if msg:
                edit_reply_markup(str(msg["chat"]["id"]), msg["message_id"])
            continue

        # предложение из очереди: завести / привязать написанием / отклонить
        if cq and data.startswith("unres:"):
            from .entities import act_on_unresolved
            from .factops import refresh_entity

            _, action, unres_id = data.split(":", 2)
            with connect() as conn:
                entity_id, answer, _context = act_on_unresolved(
                    conn, int(unres_id), action)
            answer_callback(cq["id"], answer)
            msg = cq.get("message") or {}
            if msg:
                edit_reply_markup(str(msg["chat"]["id"]), msg["message_id"])

            # пул собираем сразу же: владелец нажал кнопку и ждёт ответа,
            # а не следующего прогона по расписанию
            if entity_id:
                refresh_entity(entity_id, dry_run=False, announce=True)
                stats["generated"] = stats.get("generated", 0) + 1
            continue

        # Правка факта реплаем: владелец видит факт в аудите и присылает
        # исправленный текст. Ответ без реакции недопустим — владелец
        # не отличает «не понял» от «сломалось».
        msg = upd.get("message") or {}
        reply_to = msg.get("reply_to_message") or {}
        text = (msg.get("text") or "").strip()
        handled = False
        if text and reply_to.get("text"):
            from .factops import fact_id_in, fix

            fact_id = fact_id_in(reply_to.get("text", ""))
            if fact_id:
                handled = True
                answer = fix(fact_id, text)
                notify_owner(answer)
                stats["edited"] += 1

        if not handled and not cq:
            unhandled += 1

    if unhandled:
        log.info("Пропущено обновлений, не относящихся к ревью: %s", unhandled)
    return stats
