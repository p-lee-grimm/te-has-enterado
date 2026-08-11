"""Генерация и валидация карточек сущностей (§7.4).

Карточка создаётся только со статусом draft и попадает в канал не раньше, чем
владелец нажмёт «Ок». Причина не в вежливости: справка о живом человеке,
собранная моделью, — самый вероятный источник неверного утверждения во всём
продукте, а цена ошибки здесь выше, чем у пропущенной новости.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .config import get_settings, load_prompt
from .llm import LLMUsage, extract_json
from .textutil import longest_common_shingle, normalize_words

log = logging.getLogger(__name__)

MAX_CARD = 200

# Запрещённые формулировки (§7.4). Проверяются регуляркой до всякой модели:
# дешевле и надёжнее, чем спрашивать.
FORBIDDEN = [
    (re.compile(r"\bтоп[-\s]?\d+", re.I), "место в рейтинге"),
    (re.compile(r"№\s?\d+"), "номер в списке"),
    (re.compile(r"\b\d+[-\s]?(я|й|е)\s+(компания|место|позиц)", re.I), "место в рейтинге"),
    (re.compile(r"[$€]\s?\d|\d+\s?(млрд|млн|миллиард|миллион)", re.I),
     "абсолютные суммы"),
    (re.compile(r"\bForbes\b|\bFortune\b", re.I), "ссылка на рейтинг"),
    (re.compile(r"скандальн|противоречив|одиозн|влиятельн|легендарн|знаменит", re.I),
     "оценочная характеристика"),
    (re.compile(r"\bзамешан|причастен|уличён|махинац|коррупционер", re.I),
     "утверждение о причастности к преступлению"),
]


class CardError(RuntimeError):
    pass


def validate(card: str, source_text: str) -> list[str]:
    """Проверки, не требующие модели. Возвращает список проблем."""
    problems: list[str] = []
    card = (card or "").strip()

    if not card:
        return ["карточка пустая"]
    if len(card) > MAX_CARD:
        problems.append(f"{len(card)} символов при пределе {MAX_CARD}")

    for pattern, what in FORBIDDEN:
        if pattern.search(card):
            problems.append(f"запрещено: {what}")

    # то же правило, что и для пересказа: не переписываем источник дословно
    window = int(get_settings().require("gate.plagiarism_window_words"))
    if len(normalize_words(card)) >= window:
        match = longest_common_shingle(card, source_text, window)
        if match:
            problems.append(f"дословный кусок источника: «{match}»")

    return problems


def _llm_json(system: str, user: str, usage: LLMUsage, retries: int = 1) -> dict:
    """Вызов с ретраем на невалидный JSON: модель иногда отвечает прозой."""
    from .llm import _PROVIDERS, LLMError

    provider = get_settings().require("summarize.provider")
    fn = _PROVIDERS.get(provider)
    if fn is None:
        raise CardError(f"неизвестный провайдер LLM: {provider}")

    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return extract_json(fn(system, user, usage))
        except (LLMError, ValueError) as exc:
            last = exc
            log.warning("Карточка: ответ не разобрался (попытка %s): %s",
                        attempt + 1, str(exc)[:120])
            user += "\n\nВерни СТРОГО один JSON-объект вида {\"card\": \"...\"}."
    raise CardError(f"модель не вернула JSON: {last}")


def verify_against_source(card: str, source_text: str, usage: LLMUsage) -> list[str]:
    """Спрашивает модель, есть ли утверждения карточки в источнике.

    Отдельный дешёвый вызов: регулярка ловит форму, но не выдуманный факт.
    Проверяем целиком, а не по предложениям, — иначе вызовов становится
    столько же, сколько фраз.
    """
    system = (
        "Ты проверяешь, следует ли текст справки из источника. "
        "Ответь строго JSON: {\"ok\": true|false, \"why\": \"...\"}. "
        "ok=false, если в справке есть хотя бы одно утверждение, которого нет "
        "в источнике и которое из него не следует напрямую. "
        "Пересказ своими словами — это нормально, добавленные факты — нет."
    )
    user = f"ИСТОЧНИК:\n{source_text[:4000]}\n\nСПРАВКА:\n{card}"
    try:
        data = _llm_json(system, user, usage, retries=0)
    except Exception as exc:  # noqa: BLE001 — проверка не должна ронять генерацию
        log.warning("Сверка карточки с источником не удалась: %s", exc)
        return []
    if data.get("ok") is False:
        return [f"не следует из источника: {str(data.get('why', ''))[:120]}"]
    return []


def generate(name: str, wiki_url: str | None = None,
             context: str = "") -> dict[str, Any]:
    """Черновик карточки из Википедии.

    Возвращает {card, problems, wiki_url, source_text, cost_usd}. Карточка
    с непустым problems до канала не доходит — её показывают владельцу
    с причиной.
    """
    from . import wiki

    article = wiki.fetch_for_entity(name, wiki_url, context)
    if article is None or not article["extract"]:
        raise CardError(f"в Википедии не нашлось статьи для «{name}»")

    usage = LLMUsage()
    data = _llm_json(
        load_prompt("entity_card.md"),
        f"Имя: {name}\n\nВводная секция статьи:\n{article['extract'][:4000]}",
        usage,
    )
    card = (data.get("card") or "").strip()

    problems = validate(card, article["extract"])
    if not problems:
        problems = verify_against_source(card, article["extract"], usage)

    # Статья, найденная поиском, — отдельный риск: карточка получается
    # безупречной по форме и подтверждённой источником, но про другого
    # человека. Проверка текста этого не ловит, поэтому такую карточку
    # нельзя утвердить одним нажатием.
    if article.get("resolved_by") == "search":
        problems.append(
            "статья найдена поиском по имени — подтверди, что это тот самый"
        )

    return {
        "card": card,
        "problems": problems,
        "resolved_by": article.get("resolved_by", "url"),
        "candidates": article.get("candidates", []),
        "wiki_url": article["url"],
        "wiki_title": article["title"],
        "source_text": article["extract"],
        "cost_usd": round(usage.cost_usd, 4),
    }


# ---------------------------------------------------------------- ревью


def news_urls_for(entity: dict[str, Any]) -> list[dict[str, str]]:
    """Новости, где встретилось имя, — из очереди неразрешённых."""
    from .db import connect
    from .entities import normalize

    keys = {normalize(entity.get("name_es", "")), normalize(entity.get("name_ru", ""))}
    keys = {k for k in keys if k}
    if not keys:
        return []
    with connect() as conn:
        row = conn.execute(
            "SELECT sample_urls FROM entity_unresolved WHERE surface = ANY(%s) "
            "ORDER BY count DESC LIMIT 1",
            (list(keys),),
        ).fetchone()
    return list(row["sample_urls"]) if row and row["sample_urls"] else []


def send_for_review(entity: dict[str, Any], draft: dict[str, Any]) -> None:
    """Отправляет черновик карточки в чат ревью с кнопками."""
    import html as _html

    from .telegram import notify_owner

    draft = {**draft, "news_urls": draft.get("news_urls") or news_urls_for(entity)}

    problems = draft.get("problems") or []
    head = "⚠️ <b>Карточка не прошла проверку</b>" if problems else "<b>Новая карточка</b>"
    lines = [
        head,
        "",
        f"<b>{_html.escape(entity['name_es'])}</b> · {entity.get('type', 'other')}",
        "",
        _html.escape(draft["card"]) or "<i>(пусто)</i>",
        "",
        f"<i>{len(draft['card'])}/{MAX_CARD} символов</i>",
    ]
    if draft.get("wiki_url"):
        lines.append(f'Источник: <a href="{draft["wiki_url"]}">Википедия</a>')

    # ссылки на новости, где имя встретилось: без них решить, тот ли это
    # человек, невозможно — а именно это и надо решить
    news = draft.get("news_urls") or []
    if news:
        lines += ["", "<b>Где встретилось:</b>"]
        for n in news[:3]:
            lines.append(f'• <a href="{n["url"]}">{_html.escape(n["title"][:70])}</a>')
    if problems:
        lines += ["", "<b>Проблемы:</b>"] + [f"• {_html.escape(p)}" for p in problems]

    cands = draft.get("candidates") or []
    if len(cands) > 1:
        lines += ["", "<b>Википедия нашла ещё:</b>"]
        for c in cands[1:4]:
            lines.append(f"• {_html.escape(c['title'])} — "
                         f"<i>{_html.escape(c['description'][:60])}</i>")
        lines.append("<i>Если нужен другой — задай точный URL: "
                     "entity add … --wiki &lt;ссылка&gt;</i>")

    if problems:
        lines += ["", "<i>Кнопок нет: подтверди личность и поправь текст реплаем.</i>"]
    lines += ["", "<i>Ответь реплаем, чтобы заменить текст карточки.</i>"]

    # кнопки даём только чистому черновику: подтвердить «в один тап» то,
    # что не прошло проверку, — самый простой способ протащить ошибку
    markup = None if problems else {
        "inline_keyboard": [[
            {"text": "✅ Ок", "callback_data": f"card:ok:{entity['id']}"},
            {"text": "🗑 Удалить", "callback_data": f"card:del:{entity['id']}"},
        ]]
    }
    notify_owner("\n".join(lines), reply_markup=markup)


def _state(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM bot_state WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else None


def _set_state(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO bot_state (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, value),
    )


def process_callbacks(timeout: int = 0) -> dict[str, int]:
    """Разбирает нажатия кнопок и правки карточек реплаем.

    Вызывается из обычного прогона: держать отдельный долгоживущий процесс ради
    двух кнопок в неделю — лишняя движущаяся часть.
    """
    from .db import connect
    from .telegram import (
        TelegramError, answer_callback, edit_reply_markup, get_updates,
    )

    stats: dict[str, int] = {"approved": 0, "deleted": 0, "edited": 0}
    with connect() as conn:
        offset = _state(conn, "updates_offset")

    try:
        updates = get_updates(offset=int(offset) if offset else None, timeout=timeout)
    except TelegramError as exc:
        log.warning("getUpdates: %s", exc)
        return stats

    last = None
    unhandled = 0
    for upd in updates:
        last = upd["update_id"] + 1
        handled = False

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
            # show_alert: карточка показывается окном, а не строкой сверху
            try:
                from .telegram import _call
                _call("answerCallbackQuery", {"callback_query_id": cq["id"],
                                              "text": text, "show_alert": True})
            except Exception as exc:  # noqa: BLE001
                log.warning("answerCallbackQuery: %s", exc)
            stats["taps"] = stats.get("taps", 0) + 1
            continue

        # предложение из очереди: завести / привязать написанием / отклонить
        if cq and data.startswith("unres:"):
            _, action, unres_id = data.split(":", 2)
            from .entities import act_on_unresolved
            with connect() as conn:
                entity_id, answer = act_on_unresolved(conn, int(unres_id), action)
            answer_callback(cq["id"], answer)
            msg = cq.get("message") or {}
            if msg:
                edit_reply_markup(str(msg["chat"]["id"]), msg["message_id"])

            # карточку собираем сразу же: владелец нажал кнопку и ждёт ответа,
            # а не следующего прогона по расписанию
            if entity_id:
                with connect() as conn:
                    e = conn.execute(
                        "SELECT * FROM entities WHERE id = %s", (entity_id,)
                    ).fetchone()
                try:
                    draft = generate(e["name_es"], e["wiki_url_es"] or None)
                except CardError as exc:
                    # без статьи карточки нет, но сущность уже заведена:
                    # показываем её с причиной, текст можно прислать реплаем
                    draft = {"card": "", "problems": [str(exc)], "wiki_url": None,
                             "cost_usd": 0.0}
                else:
                    with connect() as conn:
                        conn.execute(
                            "UPDATE entities SET card=%s, wiki_url_es=%s, "
                            "card_updated_at=now() WHERE id=%s",
                            (draft["card"], draft["wiki_url"], entity_id),
                        )
                send_for_review(dict(e), draft)
                stats["generated"] = stats.get("generated", 0) + 1
            continue

        if cq and data.startswith("card:"):
            _, action, entity_id = data.split(":", 2)
            with connect() as conn:
                if action == "ok":
                    conn.execute(
                        "UPDATE entities SET card_status='approved', "
                        "card_updated_at=now() WHERE id=%s", (entity_id,))
                    stats["approved"] += 1
                elif action == "del":
                    conn.execute("DELETE FROM entities WHERE id=%s", (entity_id,))
                    stats["deleted"] += 1
            answer_callback(cq["id"], "Готово" if action == "ok" else "Удалено")
            msg = cq.get("message") or {}
            if msg:
                edit_reply_markup(str(msg["chat"]["id"]), msg["message_id"])
            continue

        # правка реплаем: текст ответа становится карточкой
        msg = upd.get("message") or {}
        handled = False
        reply_to = msg.get("reply_to_message") or {}
        text = (msg.get("text") or "").strip()
        if text and reply_to.get("text"):
            entity_id = _entity_from_review_text(reply_to.get("text", ""))
            if entity_id and len(text) <= MAX_CARD:
                with connect() as conn:
                    conn.execute(
                        "UPDATE entities SET card=%s, card_status='approved', "
                        "card_updated_at=now() WHERE id=%s", (text, entity_id))
                stats["edited"] += 1
                handled = True
                log.info("Карточка %s заменена правкой владельца", entity_id)
        if not handled and not upd.get("callback_query"):
            unhandled += 1

    if unhandled:
        # смещение всё равно двигаем — иначе одно чужое обновление
        # заблокирует очередь навсегда, — но молчать об этом нельзя
        log.info("Пропущено обновлений, не относящихся к ревью: %s", unhandled)

    if last is not None:
        with connect() as conn:
            _set_state(conn, "updates_offset", str(last))
    return stats


def _entity_from_review_text(text: str) -> str | None:
    """Находит сущность по имени из сообщения, на которое ответили реплаем."""
    from .db import connect
    from .entities import normalize

    first = ""
    for line in text.split("\n"):
        line = line.strip()
        if line and "·" in line:
            first = line.split("·")[0].strip()
            break
    if not first:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT entity_id FROM entity_aliases WHERE alias = %s LIMIT 1",
            (normalize(first),),
        ).fetchone()
    return row["entity_id"] if row else None


def mark_stale(conn) -> int:
    """Помечает stale карточки часто упоминаемых сущностей (§11).

    Редкие не трогаем: перегенерация стоит денег и внимания владельца,
    а справка о сущности, попадавшейся дважды за квартал, вряд ли устарела.
    """
    row = conn.execute(
        """
        WITH med AS (
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY mentions_count) AS m
            FROM entities WHERE card_status = 'approved'
        ), touched AS (
            UPDATE entities SET card_status = 'stale'
            WHERE card_status = 'approved'
              AND mentions_count > (SELECT m FROM med)
              AND card_updated_at < now() - interval '90 days'
            RETURNING 1
        )
        SELECT count(*) AS n FROM touched
        """
    ).fetchone()
    return row["n"]


def refresh_stale(dry_run: bool = True, limit: int = 10) -> dict[str, int]:
    """Перегенерирует устаревшие карточки и отправляет их на ревью."""
    from .db import connect

    stats = {"marked": 0, "regenerated": 0, "failed": 0}
    with connect() as conn:
        stats["marked"] = mark_stale(conn)
        rows = conn.execute(
            "SELECT * FROM entities WHERE card_status = 'stale' "
            "ORDER BY mentions_count DESC LIMIT %s",
            (limit,),
        ).fetchall()

    for e in rows:
        if dry_run:
            log.info("DRY-RUN: перегенерировали бы карточку %s", e["id"])
            continue
        try:
            draft = generate(e["name_es"], e["wiki_url_es"])
        except CardError as exc:
            log.warning("Карточка %s не перегенерировалась: %s", e["id"], exc)
            stats["failed"] += 1
            continue
        with connect() as conn:
            conn.execute(
                "UPDATE entities SET card = %s, card_status = 'draft', "
                "card_updated_at = now() WHERE id = %s",
                (draft["card"], e["id"]),
            )
        send_for_review(dict(e), draft)
        stats["regenerated"] += 1

    log.info("Обновление карточек: помечено %s, перегенерировано %s, ошибок %s",
             stats["marked"], stats["regenerated"], stats["failed"])
    return stats
