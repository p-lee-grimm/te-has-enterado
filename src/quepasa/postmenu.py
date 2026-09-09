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
    ("ctx",  "📇 Добавить контекст"),
    ("tr",   "✍️ Перевести заново"),
    ("src",  "📰 Дополнить источниками"),
    ("rel",  "🔗 Связать с другой"),
    ("dup",  "🔁 Дубликат"),
    ("del",  "🗑 Снять"),
]


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
    """Собирает пояснения к именам этого поста и вставляет их в него."""
    from .db import connect
    from .entities import mark_entities
    from .factops import refresh_entity
    from .facts import has_pool
    from .posts import backfill_entity_context

    with connect() as conn:
        post = _post(conn, post_id)
        if post is None:
            return "Поста нет в базе."
        head = post["header_md"] or ""
        # берём заведённые сущности, чьё имя действительно есть в шапке:
        # отбор тем же mark_entities, что потом поставит звёздочку
        rows = conn.execute(
            "SELECT * FROM entities WHERE NOT never_explain"
        ).fetchall()
        present = [dict(r) for r in rows
                   if mark_entities(head, [dict(r)]) != head]
        shown = set(post.get("entity_context") or {})

    todo = [e for e in present if e["id"] not in shown]
    if not todo:
        return ("В шапке нет заведённых имён без пояснения. "
                "Если имя новое — оно попадёт в очередь и заведётся само.")

    added = 0
    for e in todo[:3]:
        with connect() as conn:
            if not has_pool(conn, e["id"]):
                refresh_entity(e["id"], dry_run=False, announce=False)
        res = backfill_entity_context(e["id"], dry_run=False)
        added += int(res.get("edited", 0))

    names = ", ".join(html.escape(e["name_es"]) for e in todo[:3])
    return (f"Пояснения собраны: {names}.\n"
            f"Постов дополнено: {added}." if added else
            f"По именам {names} фактов под тему поста не нашлось — "
            f"пояснения не будет, это штатный исход.")


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


def run_action(code: str, post_id: int, extra: int | None = None):
    """Выполняет действие. Возвращает текст либо (текст, клавиатура)."""
    from .posts import unpublish

    if code == "ctx":
        return add_context(post_id)
    if code == "tr":
        return retranslate(post_id)
    if code == "src":
        return add_sources(post_id)
    if code == "rel":
        return related_candidates(post_id)
    if code == "reldo" and extra is not None:
        return link_related(post_id, extra)
    if code == "dup":
        return mark_duplicate(post_id)
    if code == "del":
        return unpublish(post_id)
    if code == "nope":
        return "Оставили как есть."
    return f"Не знаю действия {html.escape(code)}."
