"""Действия над вышедшим постом из служебного чата.

Канал сам становится интерфейсом: владелец видит проблему в посте,
пересылает его в чат ревью или кидает ссылку — и получает список того,
что с этим постом можно сделать. Читать логи и помнить номера сюжетов
для этого не нужно.

Каждое действие выполняется сразу и отвечает результатом: подтверждать
дважды нечего, а откатить любое из них можно тем же способом.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

# Ссылка на пост канала: и публичная, и внутренняя форма t.me/c/<id>/<msg>.
_LINK = re.compile(r"t\.me/(?:c/\d+|[A-Za-z][\w_]{3,})/(\d+)")

ACTIONS = [
    ("ctx",  "📇 Контекст"),
    ("tr",   "✍️ Перевести заново"),
    ("src",  "📰 Дополнить источниками"),
    ("rel",  "🔗 Связать с другой"),
    ("dup",  "🔁 Дубликат"),
]
# «Снять» здесь нет намеренно: удалить пост в канале — два тапа, а
# переслать его боту и нажать кнопку строго больше работы. Дубликат
# остаётся, потому что делает то, чего канал не умеет: помечает сюжет,
# чтобы он не вышел снова.


def message_id_in(msg: dict[str, Any]) -> int | None:
    """Номер поста канала: из пересылки или из ссылки в тексте.

    Пересылку берём по forward_origin: у канала там лежит собственный
    message_id, а не номер копии в служебном чате.
    """
    origin = msg.get("forward_origin") or {}
    if origin.get("type") == "channel" and origin.get("message_id"):
        return int(origin["message_id"])
    if msg.get("forward_from_message_id"):  # старый формат Bot API
        return int(msg["forward_from_message_id"])

    m = _LINK.search(msg.get("text") or msg.get("caption") or "")
    return int(m.group(1)) if m else None


def menu(message_id: int) -> tuple[str, dict[str, Any]] | None:
    """Сообщение со списком действий над постом."""
    from .db import connect
    from .telegram import message_link

    with connect() as conn:
        post = conn.execute(
            "SELECT id, cluster_id, header_md, status FROM posts "
            "WHERE message_id = %s", (message_id,),
        ).fetchone()
    if post is None:
        return None

    head = (post["header_md"] or "").split("\n")[0].strip("* ")
    link = message_link(message_id)
    text = "\n".join([
        f'<a href="{link}">Пост {message_id}</a>',
        html.escape(head[:150]),
        "",
        "<i>Что с ним сделать?</i>",
    ])
    rows, pair = [], []
    for code, label in ACTIONS:
        pair.append({"text": label, "callback_data": f"pa:{code}:{post['id']}"})
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    return text, {"inline_keyboard": rows}


# ------------------------------------------------------------- сами действия


def _post(conn, post_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM posts WHERE id = %s", (post_id,)).fetchone()
    return dict(row) if row else None


def add_context(post_id: int) -> str:
    """Собирает пояснения к именам этого поста и вставляет их в него.

    Имена берём из самой шапки: латиница в русском тексте — по нашему же
    правилу имя собственное. Незаведённые заводим прямо здесь, а не ждём,
    пока их подберёт очередь: владелец нажал кнопку именно потому, что
    пояснение нужно сейчас.
    """
    from .db import connect
    from .entities import adopt_name, latin_names_in
    from .factops import refresh_entity
    from .facts import has_pool
    from .posts import backfill_entity_context

    with connect() as conn:
        post = _post(conn, post_id)
        if post is None:
            return "Поста нет в базе."
        head = post["header_md"] or ""
        shown = set(post.get("entity_context") or {})

        names = latin_names_in(head)
        if not names:
            return ("В шапке нет имён латиницей — пояснять нечего. "
                    "Если имя записано кириллицей, это ошибка перевода: "
                    "жми «Перевести заново».")

        targets: list[tuple[str, str]] = []   # (entity_id, как показать)
        created: list[str] = []
        for name in names:
            entity_id, is_new = adopt_name(conn, name)
            if not entity_id:
                continue
            row = conn.execute(
                "SELECT name_es, never_explain FROM entities WHERE id = %s",
                (entity_id,),
            ).fetchone()
            if row is None or row["never_explain"]:
                continue          # короля и премьера поясняет роль в тексте
            if entity_id in shown:
                continue          # пояснение уже стоит
            targets.append((entity_id, row["name_es"]))
            if is_new:
                created.append(row["name_es"])

    if not targets:
        return ("Всё, что можно пояснить, уже пояснено: остальные имена "
                "либо помечены как заведомо знакомые, либо уже в блоке.")

    lines: list[str] = []
    edited = 0
    for entity_id, shown_name in targets[:3]:
        with connect() as conn:
            pool = has_pool(conn, entity_id)
        if not pool:
            refresh_entity(entity_id, dry_run=False, announce=False)
        res = backfill_entity_context(entity_id, dry_run=False)
        n = int(res.get("edited", 0))
        edited += n
        lines.append(f"• <b>{html.escape(shown_name)}</b> — "
                     + ("пояснение в посте" if n else
                        "проверенных фактов не нашлось, пояснения не будет"))

    head_line = (f"Завёл: {', '.join(html.escape(n) for n in created)}\n"
                 if created else "")
    tail = ("" if edited else
            "\n\n<i>Источников по этим именам нет — это штатный исход. "
            "Можно дать текст руками: /fix и номер поста.</i>")
    return f"{head_line}" + "\n".join(lines) + tail


def context_menu(post_id: int) -> tuple[str, dict[str, Any]] | str:
    """Что сейчас в блоке «кто это» и что с ним можно сделать.

    Одной кнопкой тут не обойтись: пояснение бывает не только отсутствующим,
    но и неудачным — собранным не под ту тему, устаревшим или просто корявым
    («20 автономных вехикулей»). Владелец видит блок в канале и должен
    из канала же его и починить, не открывая консоль.
    """
    from .db import connect
    from .entities import context_text

    with connect() as conn:
        post = _post(conn, post_id)
        if post is None:
            return "Поста нет в базе."
        shown = []
        for entity_id in (post.get("entity_context") or {}):
            text, _url = context_text(post.get("entity_context"), entity_id)
            if not text:
                continue
            row = conn.execute("SELECT name_es FROM entities WHERE id = %s",
                               (entity_id,)).fetchone()
            shown.append((row["name_es"] if row else entity_id, text))

    if shown:
        lines = ["<b>Сейчас в посте:</b>"]
        for name, text in shown:
            lines.append(f"• <b>{html.escape(name)}</b> — {html.escape(text[:180])}")
    else:
        lines = ["<i>Пояснений в посте сейчас нет.</i>"]

    rows = [[
        {"text": "➕ Собрать", "callback_data": f"pa:ctxadd:{post_id}"},
        {"text": "🔄 Пересобрать", "callback_data": f"pa:ctxre:{post_id}"},
    ]]
    if shown:
        rows.append([
            {"text": "📋 Откуда это", "callback_data": f"pa:ctxwhy:{post_id}"},
            {"text": "🗑 Убрать", "callback_data": f"pa:ctxdel:{post_id}"},
        ])
    return "\n".join(lines), {"inline_keyboard": rows}


def rebuild_context(post_id: int) -> str:
    """Собирает пояснения заново для тех же имён, поверх старых.

    Отдельно от «Собрать»: та кнопка ищет новые имена и не трогает уже
    показанное, а здесь смысл обратный — текст есть, но он плохой.
    """
    from .db import connect
    from .facts import build_context
    from .llm import LLMUsage
    from .posts import refresh_one

    with connect() as conn:
        post = _post(conn, post_id)
        if post is None:
            return "Поста нет в базе."
        ids = list((post.get("entity_context") or {}).keys()) or \
            list(post.get("entity_ids") or [])
        if not ids:
            return "Пояснять нечего: в посте нет сущностей. Жми «Собрать»."
        saved = conn.execute("SELECT * FROM entities WHERE id = ANY(%s)",
                             (ids,)).fetchall()

    usage = LLMUsage()
    contexts: dict[str, Any] = {}
    headline = (post["header_md"] or "").split("\n")[0].strip("* ")
    for ent in saved:
        with connect() as conn:
            built = build_context(conn, dict(ent), post["category"] or "",
                                  headline, usage)
        if built:
            contexts[ent["id"]] = built

    if not contexts:
        return ("Собрать заново не вышло: подходящих проверенных фактов "
                "под тему поста нет. Старый текст оставил на месте.")

    import json

    with connect() as conn:
        conn.execute(
            "UPDATE posts SET entity_context = %s, entity_ids = %s WHERE id = %s",
            (json.dumps(contexts, ensure_ascii=False),
             json.dumps(list(contexts)), post_id),
        )
    if not refresh_one(post_id):
        return "Пересобрал в базе, но в канал правка не ушла — смотри лог."

    lines = ["<b>Пересобрано:</b>"]
    for entity_id, built in contexts.items():
        lines.append(f"• {html.escape(built['context'][:180])}")
    return "\n".join(lines)


def drop_context(post_id: int) -> str:
    """Убирает блок «кто это» из поста целиком."""
    from .db import connect
    from .posts import refresh_one

    with connect() as conn:
        post = _post(conn, post_id)
        if post is None:
            return "Поста нет в базе."
        if not (post.get("entity_context") or post.get("entity_ids")):
            return "Блока и так нет."
        conn.execute(
            "UPDATE posts SET entity_context = '{}'::jsonb, "
            "entity_ids = '[]'::jsonb WHERE id = %s",
            (post_id,),
        )
    return ("Блок убран из поста."
            if refresh_one(post_id) else "Убрал в базе, но пост не пересобрался.")


def context_sources(post_id: int) -> tuple[str, dict[str, Any] | None] | str:
    """Факты, на которых стоит пояснение, и ссылка на источник.

    Нужна, когда текст в блоке выглядит странно: видно, из какого факта он
    собран и откуда факт взят, — и оттуда же можно запретить пояснять имя.
    Номер факта показываем: им правят пул через `manage.py fact fix`.
    """
    from .db import connect

    with connect() as conn:
        post = _post(conn, post_id)
        if post is None:
            return "Поста нет в базе.", None
        blocks = post.get("entity_context") or {}
        if not blocks:
            return "Пояснений в посте нет — показывать нечего.", None

        lines, buttons = [], []
        for entity_id, block in blocks.items():
            if not isinstance(block, dict):
                continue
            row = conn.execute("SELECT name_es FROM entities WHERE id = %s",
                               (entity_id,)).fetchone()
            name = row["name_es"] if row else entity_id
            lines.append(f"<b>{html.escape(name)}</b>")
            facts = conn.execute(
                "SELECT id, fact, source_url FROM entity_facts "
                "WHERE id = ANY(%s) ORDER BY id",
                (list(block.get("fact_ids") or []),),
            ).fetchall()
            for f in facts:
                src = f" <a href=\"{f['source_url']}\">источник</a>" if f["source_url"] else ""
                lines.append(f"  #{f['id']} {html.escape(f['fact'][:160])}{src}")
            if not facts:
                lines.append("  <i>факты, на которых стоял текст, из пула ушли</i>")
            buttons.append([{"text": f"🙈 Не пояснять {name[:20]}",
                             "callback_data": f"pa:ctxnever:{post_id}:{entity_id}"}])

    lines.append("")
    lines.append("<i>Поправить текст факта: manage.py fact fix НОМЕР «новый текст»</i>")
    return "\n".join(lines), {"inline_keyboard": buttons} if buttons else None


def never_explain(post_id: int, entity_id: str) -> str:
    """Помечает имя как заведомо знакомое и убирает его пояснение из поста.

    Пометка глобальная: короля, премьера и Real Madrid поясняют роль в тексте,
    и пояснение к ним — шум во всех постах, а не только в этом.
    """
    import json

    from .db import connect
    from .posts import refresh_one

    with connect() as conn:
        post = _post(conn, post_id)
        if post is None:
            return "Поста нет в базе."
        row = conn.execute(
            "UPDATE entities SET never_explain = TRUE WHERE id = %s "
            "RETURNING name_es", (entity_id,),
        ).fetchone()
        if row is None:
            return "Такой сущности нет."
        contexts = {k: v for k, v in (post.get("entity_context") or {}).items()
                    if k != entity_id}
        ids = [e for e in (post.get("entity_ids") or []) if e != entity_id]
        conn.execute(
            "UPDATE posts SET entity_context = %s, entity_ids = %s WHERE id = %s",
            (json.dumps(contexts, ensure_ascii=False), json.dumps(ids), post_id),
        )
    refresh_one(post_id)
    return (f"<b>{html.escape(row['name_es'])}</b> больше не поясняем — "
            "ни здесь, ни в будущих постах. Вернуть: "
            f"<code>manage.py entity never-explain {html.escape(entity_id)} --off</code>")


def retranslate(post_id: int) -> str:
    """Пересобирает шапку моделью заново и правит пост."""
    from .db import connect
    from .posts import generate_header, refresh_one

    with connect() as conn:
        post = _post(conn, post_id)
        if post is None:
            return "Поста нет в базе."
        old = (post["header_md"] or "").split("\n")[0].strip("* ")

    try:
        header, _topic, _meta = generate_header(post["cluster_id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("Перевод поста %s не пересобрался: %s", post_id, exc)
        return f"Не получилось: {html.escape(str(exc)[:150])}"

    # UPD-строки переживают перевод: они про факты, а не про формулировку
    upds = [ln for ln in (post["header_md"] or "").split("\n")
            if ln.strip().startswith(("_UPD (", "UPD ("))]
    if upds:
        header = header + "\n\n" + "\n\n".join(upds)

    with connect() as conn:
        conn.execute("UPDATE posts SET header_md = %s WHERE id = %s",
                     (header, post_id))
    if not refresh_one(post_id):
        return "Текст пересобран, но в канал не ушёл — смотри лог."
    new = header.split("\n")[0].strip("* ")
    return (f"<b>Было:</b> {html.escape(old)}\n"
            f"<b>Стало:</b> {html.escape(new)}\n\n"
            f"<i>Не лучше — /fix {post['message_id']} и свой текст.</i>")


def add_sources(post_id: int) -> str:
    """Догоняет пост изданиями, вышедшими после публикации."""
    from .db import connect
    from .posts import sync_post

    with connect() as conn:
        post = _post(conn, post_id)
    if post is None:
        return "Поста нет в базе."

    res = sync_post(post["cluster_id"], dry_run=False)
    status = res.get("status")
    if status == "edited":
        return f"Добавлено: {', '.join(res.get('added') or []) or '—'}"
    if status == "frozen":
        return f"Окно правки закрыто: {res.get('reason', '')}"
    return "Новых изданий по этому сюжету нет."


def related_candidates(post_id: int) -> tuple[str, dict[str, Any] | None]:
    """Предлагает, с чем связать пост: выбор за владельцем."""
    from .db import connect
    from .related import find_related

    with connect() as conn:
        post = _post(conn, post_id)
        if post is None:
            return "Поста нет в базе.", None
        rel = find_related(conn, post["cluster_id"], post.get("category"),
                           list(post.get("entity_ids") or []))

    if rel is None:
        return ("Похожего сюжета с постом не нашлось. "
                "Связать вручную пока нельзя — скажи, если нужно."), None

    text = (f"Похоже на: <b>{html.escape(rel['headline'][:120])}</b>\n"
            f"<i>совпадение {rel['sim']:.2f}, по признаку «{rel['why']}»</i>")
    kb = {"inline_keyboard": [[
        {"text": "🔗 Связать", "callback_data": f"pa:reldo:{post_id}:{rel['cluster_id']}"},
        {"text": "✖️ Нет", "callback_data": f"pa:nope:{post_id}"},
    ]]}
    return text, kb


def link_related(post_id: int, other_cluster_id: int) -> str:
    """Ставит связь и пересобирает пост со строкой «Ранее по теме»."""
    from .db import connect
    from .posts import refresh_one
    from .related import link, render_link_md

    with connect() as conn:
        post = _post(conn, post_id)
        if post is None:
            return "Поста нет в базе."
        link(conn, post["cluster_id"], other_cluster_id)
        other = conn.execute(
            "SELECT c.id, p.message_id, p.header_md FROM clusters c "
            "JOIN posts p ON p.cluster_id = c.id WHERE c.id = %s",
            (other_cluster_id,),
        ).fetchone()
        if other is None:
            return "Связал, но у второго сюжета нет поста — ссылку не поставить."
        rel_md = render_link_md({
            "cluster_id": other["id"], "message_id": other["message_id"],
            "headline": (other["header_md"] or "").split("\n")[0].strip("* "),
        })
        conn.execute("UPDATE posts SET related_md = %s WHERE id = %s",
                     (rel_md, post_id))

    return ("Связано, ссылка в посте."
            if refresh_one(post_id) else "Связал, но пост не пересобрался.")


def mark_duplicate(post_id: int) -> str:
    """Снимает пост и запрещает сюжету выходить снова."""
    from .db import connect
    from .posts import unpublish

    with connect() as conn:
        post = _post(conn, post_id)
        if post is None:
            return "Поста нет в базе."
        cluster_id = post["cluster_id"]

    answer = unpublish(post_id)
    with connect() as conn:
        # сюжет остаётся в базе, но пост по нему больше не выйдет
        conn.execute(
            "UPDATE clusters SET last_published_at = now() WHERE id = %s",
            (cluster_id,),
        )
    log.info("Пост %s снят как дубликат", post_id)
    return f"{answer}. Сюжет помечен как уже выходивший."


def run_action(code: str, post_id: int, extra: str | int | None = None):
    """Выполняет действие. Возвращает текст либо (текст, клавиатура)."""
    if code == "ctx":
        return context_menu(post_id)
    if code == "ctxadd":
        return add_context(post_id)
    if code == "ctxre":
        return rebuild_context(post_id)
    if code == "ctxdel":
        return drop_context(post_id)
    if code == "ctxwhy":
        return context_sources(post_id)
    if code == "ctxnever" and extra is not None:
        return never_explain(post_id, str(extra))
    if code == "tr":
        return retranslate(post_id)
    if code == "src":
        return add_sources(post_id)
    if code == "rel":
        return related_candidates(post_id)
    if code == "reldo" and extra is not None:
        return link_related(post_id, int(extra))
    if code == "dup":
        return mark_duplicate(post_id)
    if code == "nope":
        return "Оставили как есть."
    return f"Не знаю действия {html.escape(code)}."
