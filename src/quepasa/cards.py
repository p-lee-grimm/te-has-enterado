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


def news_source_text(conn, name: str, limit: int = 8) -> str:
    """Заголовки и подводки новостей, где встретилось имя.

    Источник для карточки, когда в Википедии статьи нет. Полные тексты живут
    72 часа, поэтому берём то, что переживает уборку: заголовок и подводку
    из ленты.
    """
    rows = conn.execute(
        """
        SELECT a.title, coalesce(a.summary_feed, '') AS summary, s.name AS source
        FROM articles a JOIN sources s ON s.id = a.source_id
        WHERE a.title ILIKE %s OR a.summary_feed ILIKE %s OR a.body ILIKE %s
        ORDER BY a.published_at DESC NULLS LAST
        LIMIT %s
        """,
        (f"%{name}%", f"%{name}%", f"%{name}%", limit),
    ).fetchall()
    return "\n\n".join(
        f"[{r['source']}] {r['title']}\n{r['summary'][:400]}".strip() for r in rows
    )


def generate_from_knowledge(name: str, hint_text: str = "") -> dict[str, Any]:
    """Карточка по знаниям модели, когда в Википедии статьи нет.

    Сверки с источником здесь нет и быть не может: источник — сама модель.
    Владелец видит пометку и правит текст реплаем, если что-то не так, —
    это его осознанный выбор в обмен на то, что карточка вообще появится.

    Отрывки новостей идут в подсказку не как источник фактов, а чтобы
    отличить нужного человека от однофамильца.
    """
    usage = LLMUsage()
    user = f"Имя: {name}"
    if hint_text.strip():
        user += f"\n\nГде встретилось (для опознания, не как источник):\n{hint_text[:4000]}"

    data = _llm_json(load_prompt("entity_card_claude.md"), user, usage)
    card = (data.get("card") or "").strip()
    if not card:
        raise CardError("модель не уверена, кто это — нужен текст карточки от тебя")

    # Проверяем только форму: длина и запрещённые обороты. Сверять с текстом
    # новостей нельзя — модель писала не по ним, и верные факты отсеялись бы.
    problems = validate(card, "")
    return {"card": card, "problems": problems, "wiki_url": None,
            "from_model": True, "cost_usd": usage.cost_usd}


def send_for_review(entity: dict[str, Any], draft: dict[str, Any]) -> None:
    """Отправляет черновик карточки в чат ревью с кнопками."""
    import html as _html

    from .telegram import notify_owner

    draft = {**draft, "news_urls": draft.get("news_urls") or news_urls_for(entity)}

    problems = draft.get("problems") or []
    if problems:
        head = "⚠️ <b>Карточка не прошла проверку</b>"
    elif draft.get("from_model"):
        # источник — знания модели, сверять не с чем; владелец знает и проверит
        head = "🤖 <b>Карточка со слов модели</b>"
    else:
        head = "<b>Новая карточка</b>"
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
    elif draft.get("from_model"):
        lines.append("<i>Источник — знания модели, не сверено со статьёй. "
                     "Проверь и поправь реплаем, если что-то не так.</i>")

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
        lines += ["", "<i>Утвердить нельзя, пока не подтверждена личность. "
                      "Пришли реплаем ссылку на статью Википедии или текст "
                      "карточки — или собери через Claude.</i>"]
    else:
        lines += ["", "<i>Ответь реплаем, чтобы заменить текст карточки.</i>"]

    # Утверждение «в один тап» даём только чистому черновику: подтвердить
    # одним нажатием то, что не прошло проверку, — простейший способ
    # протащить ошибку. Карточка со слов модели проверку проходит: она
    # помечена, и владелец решил, что правит её сам.
    # Пересборка — не утверждение, поэтому доступна всегда.
    rows = []
    if not problems:
        rows.append([
            {"text": "✅ Ок", "callback_data": f"card:ok:{entity['id']}"},
            {"text": "🗑 Удалить", "callback_data": f"card:del:{entity['id']}"},
        ])
    rows.append([
        {"text": "🤖 Собрать через Claude", "callback_data": f"card:gen:{entity['id']}"},
    ])
    if problems:
        rows[-1].append(
            {"text": "🗑 Удалить", "callback_data": f"card:del:{entity['id']}"})
    notify_owner("\n".join(lines), reply_markup={"inline_keyboard": rows})


_WIKI_RE = re.compile(r"https?://[a-z]{2,3}\.(?:m\.)?wikipedia\.org/wiki/\S+", re.I)
_URL_RE = re.compile(r"^\s*https?://\S+\s*$", re.I)


def wiki_url_in(text: str) -> str | None:
    """Ссылка на статью Википедии из ответа владельца.

    Якорь отрезаем: браузер добавляет к скопированной ссылке «#:~:text=…»
    с подсвеченной цитатой, а из адреса мы берём заголовок статьи — с хвостом
    он превратится в несуществующий.
    """
    m = _WIKI_RE.search(text or "")
    return m.group(0).split("#")[0] if m else None


def looks_like_url(text: str) -> bool:
    return bool(_URL_RE.match(text or ""))


def _regenerate_from_url(entity_id: str, wiki_url: str) -> None:
    """Пересобирает карточку по подтверждённой владельцем статье.

    Статья, заданная ссылкой, не требует подтверждения личности — это её и
    подтвердило, — поэтому пересобранная карточка приходит уже с кнопками.
    """
    from .db import connect
    from .telegram import notify_owner

    with connect() as conn:
        e = conn.execute(
            "SELECT * FROM entities WHERE id = %s", (entity_id,)
        ).fetchone()
    if e is None:
        notify_owner(f"Сущности <code>{entity_id}</code> уже нет.")
        return

    # Не только CardError: Википедия отвечает и 403, и 429, и это прилетает
    # TransientError. Одна недоступная статья не должна ронять разбор нажатий.
    try:
        draft = generate(e["name_es"], wiki_url)
    except Exception as exc:  # noqa: BLE001
        log.warning("Карточка %s по ссылке не собралась: %s", entity_id, exc)
        notify_owner(f"По этой ссылке карточка не собралась: {exc}")
        return

    with connect() as conn:
        conn.execute(
            "UPDATE entities SET card=%s, wiki_url_es=%s, card_status='draft', "
            "card_updated_at=now() WHERE id=%s",
            (draft["card"], draft["wiki_url"], entity_id),
        )
    log.info("Карточка %s пересобрана по ссылке владельца", entity_id)
    send_for_review(dict(e), draft)


def _backfill_and_report(entity_id: str) -> None:
    """Разносит утверждённую карточку по вышедшим постам и отчитывается."""
    from .posts import backfill_entity_card
    from .telegram import notify_owner

    try:
        res = backfill_entity_card(entity_id, dry_run=False)
    except Exception as exc:  # noqa: BLE001 — утверждение уже состоялось
        log.warning("Карточка %s: разнести по постам не вышло: %s", entity_id, exc)
        notify_owner(f"Карточка <code>{entity_id}</code> утверждена, но в старые "
                     f"посты не добавилась: {exc}")
        return

    if res.get("edited"):
        parts = [f"Карточка <b>{entity_id}</b> добавлена в {res['edited']} "
                 f"уже вышедших постов."]
        if res.get("skipped_full"):
            parts.append(f"Ещё {res['skipped_full']} пропущено — там уже "
                         f"две карточки.")
        if res.get("errors"):
            parts.append(f"Не поправилось: {res['errors']}.")
        notify_owner(" ".join(parts))


def _regenerate_from_model(entity_id: str) -> None:
    """Пересобирает карточку по знаниям модели и отправляет на ревью."""
    from .db import connect
    from .telegram import notify_owner

    with connect() as conn:
        e = conn.execute(
            "SELECT * FROM entities WHERE id = %s", (entity_id,)
        ).fetchone()
        if e is None:
            notify_owner(f"Сущности <code>{entity_id}</code> уже нет.")
            return
        # только для опознания: у испанских имён много однофамильцев
        hint = news_source_text(conn, e["name_es"])

    try:
        draft = generate_from_knowledge(e["name_es"], hint)
    except Exception as exc:  # noqa: BLE001 — одна неудача не роняет разбор
        log.warning("Карточка %s не собралась: %s", entity_id, exc)
        notify_owner(f"<b>{e['name_es']}</b>: {exc}")
        return

    with connect() as conn:
        conn.execute(
            "UPDATE entities SET card=%s, card_status='draft', "
            "card_updated_at=now() WHERE id=%s", (draft["card"], entity_id),
        )
    log.info("Карточка %s собрана моделью", entity_id)
    send_for_review(dict(e), draft)


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
    # вернётся к тому же нажатию в следующий раз — карточка пересобирается
    # заново, за модель платим снова, и так по кругу каждые две минуты.
    # Потерять одно нажатие при сбое дешевле, чем зациклить его навсегда.
    if updates:
        with connect() as conn:
            _set_state(conn, "updates_offset", str(updates[-1]["update_id"] + 1))

    unhandled = 0
    for upd in updates:
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
                entity_id, answer, context = act_on_unresolved(
                    conn, int(unres_id), action)
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
                    draft = generate(e["name_es"], e["wiki_url_es"] or None, context)
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

            # «news» — данные кнопок, разосланных до переименования: сообщения
            # уже лежат в чате, и ломать их нельзя
            if action in ("gen", "news"):
                # отвечаем сразу: сборка идёт в модель и занимает секунды,
                # а callback_query столько не ждёт
                answer_callback(cq["id"], "Собираю карточку…")
                msg = cq.get("message") or {}
                if msg:
                    edit_reply_markup(str(msg["chat"]["id"]), msg["message_id"])
                _regenerate_from_model(entity_id)
                stats["generated"] = stats.get("generated", 0) + 1
                continue

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
            if action == "ok":
                _backfill_and_report(entity_id)
            continue

        # Ответ реплаем: либо ссылка на статью — это подтверждение личности,
        # либо текст — это готовая карточка. Ответ без реакции недопустим:
        # владелец не отличает «не понял» от «сломалось».
        msg = upd.get("message") or {}
        handled = False
        reply_to = msg.get("reply_to_message") or {}
        text = (msg.get("text") or "").strip()
        if text and reply_to.get("text"):
            entity_id = _entity_from_review_text(reply_to.get("text", ""))
            wiki = wiki_url_in(text)
            if entity_id and wiki:
                handled = True
                stats["confirmed"] = stats.get("confirmed", 0) + 1
                _regenerate_from_url(entity_id, wiki)
            elif entity_id and looks_like_url(text):
                # не Википедия — карточкой это не станет ни при каком раскладе
                handled = True
                notify_owner("Ссылка не на Википедию — карточку из неё не собрать. "
                             "Пришли ссылку на статью или текст карточки.")
            elif entity_id and len(text) > MAX_CARD:
                handled = True
                notify_owner(f"Слишком длинно: {len(text)} символов при "
                             f"пределе {MAX_CARD}. Карточка не заменена.")
            elif entity_id:
                with connect() as conn:
                    conn.execute(
                        "UPDATE entities SET card=%s, card_status='approved', "
                        "card_updated_at=now() WHERE id=%s", (text, entity_id))
                stats["edited"] += 1
                handled = True
                log.info("Карточка %s заменена правкой владельца", entity_id)
                notify_owner(f"Карточка <code>{entity_id}</code> заменена и утверждена.")
        if not handled and not upd.get("callback_query"):
            unhandled += 1

    if unhandled:
        log.info("Пропущено обновлений, не относящихся к ревью: %s", unhandled)
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
