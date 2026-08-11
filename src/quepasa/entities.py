"""Сущности: матчинг, правила показа, рендер карточек (§7).

Карточка отвечает только на «кто это». Почему новость важна — это
significance в теле поста: одна и та же сущность в разных сюжетах важна
по-разному.

Автосоздание сущностей запрещено. Ошибка матчинга даёт дубликат, дубликаты
расходятся в фактах, разгребать дороже, чем завести руками по три штуки
в неделю. Всё несматченное идёт в очередь entity_unresolved.
"""

from __future__ import annotations

import html
import logging
import re
import unicodedata
from typing import Any

from .config import get_settings

log = logging.getLogger(__name__)

# Префиксы должностей и артикли: «президент Санчес» и «Санчес» — одно лицо.
_PREFIXES = (
    "el", "la", "los", "las", "don", "doña",
    "presidente", "president", "presidenta", "ministro", "ministra",
    "министр", "президент", "председатель", "глава", "лидер", "экс",
    "vicepresidente", "vicepresidenta", "líder", "lider",
)


def normalize(surface: str) -> str:
    """Нижний регистр, без диакритики, без артиклей и должностных префиксов."""
    text = unicodedata.normalize("NFD", (surface or "").casefold())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^\w\s-]", " ", text)
    words = [w for w in text.split() if w]
    while words and words[0] in _PREFIXES:
        words.pop(0)
    return " ".join(words)


def _levenshtein(a: str, b: str, cap: int = 3) -> int:
    """Расстояние с ранним выходом: длинные хвосты нас не интересуют."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def match(conn, surface: str) -> tuple[str | None, str | None]:
    """(entity_id, кандидат). Точное совпадение -> id; похожее -> кандидат.

    Нечёткое совпадение НЕ создаёт сущность и не подставляет её в пост:
    оно только подсказывает владельцу в очереди неразрешённых.
    """
    key = normalize(surface)
    if not key:
        return None, None

    row = conn.execute(
        "SELECT entity_id FROM entity_aliases WHERE alias = %s LIMIT 1", (key,)
    ).fetchone()
    if row:
        return row["entity_id"], None

    # Похоже по написанию или по фамилии. Короткие строки нечётко не сравниваем:
    # у аббревиатуры из трёх букв расстояние 2 до половины словаря — «ACS»
    # оказывается «похоже на» и El País, и ABC. Подсказка в очереди должна
    # помогать, а не сбивать.
    min_len = int(get_settings().get_path("entities.fuzzy_min_length", 5))
    cap = 1 if len(key) < 8 else 2

    rows = conn.execute("SELECT entity_id, alias FROM entity_aliases").fetchall()
    surname = key.split()[-1] if key.split() else ""

    best: tuple[int, str] | None = None
    for r in rows:
        alias = r["alias"]
        # совпадение по фамилии надёжнее опечаточного и идёт вперёд
        if surname and len(surname) > 3 and surname in alias.split():
            return None, r["entity_id"]
        if len(key) < min_len or len(alias) < min_len:
            continue
        d = _levenshtein(key, alias, cap)
        if d <= cap and (best is None or d < best[0]):
            best = (d, r["entity_id"])
    return None, best[1] if best else None


def remember_unresolved(conn, surface: str, candidate: str | None,
                        context: str = "", cluster_id: int | None = None) -> None:
    """Кладёт имя в очередь вместе со ссылками на новости, где оно встретилось.

    Без ссылок предложение завести сущность нечем оценить: чтобы понять, кто
    такой человек с распространённой фамилией, надо сначала прочитать новость,
    в которой он появился.
    """
    import json as _json

    urls: list[dict[str, str]] = []
    if cluster_id is not None:
        rows = conn.execute(
            """
            SELECT a.title, COALESCE(a.url, a.url_canonical) AS url, s.name
            FROM articles a JOIN sources s ON s.id = a.source_id
            WHERE a.cluster_id = %s
            ORDER BY a.published_at DESC LIMIT 3
            """,
            (cluster_id,),
        ).fetchall()
        urls = [{"title": r["title"], "url": r["url"], "source": r["name"]}
                for r in rows if r["url"]]

    conn.execute(
        """
        INSERT INTO entity_unresolved (surface, candidate_id, sample_context, sample_urls)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (surface) DO UPDATE
            SET count = entity_unresolved.count + 1,
                last_seen = now(),
                candidate_id = COALESCE(entity_unresolved.candidate_id, EXCLUDED.candidate_id),
                -- ссылки держим от первой встречи: она и есть повод завести
                sample_urls = CASE
                    WHEN entity_unresolved.sample_urls = '[]'::jsonb
                    THEN EXCLUDED.sample_urls ELSE entity_unresolved.sample_urls END
        """,
        (normalize(surface), candidate, context[:300], _json.dumps(urls, ensure_ascii=False)),
    )


def resolve_all(conn, extracted: list[dict[str, Any]], cluster_id: int | None = None
                ) -> list[dict[str, Any]]:
    """Сопоставляет то, что вернула модель, с реестром.

    Возвращает найденные сущности с их salience. Ненайденное уходит в очередь.
    """
    found: list[dict[str, Any]] = []
    for item in extracted or []:
        surface = (item.get("surface") or "").strip()
        if not surface:
            continue
        entity_id, candidate = match(conn, surface)
        if entity_id is None:
            remember_unresolved(conn, surface, candidate,
                                item.get("name_ru", ""), cluster_id)
            continue
        row = conn.execute("SELECT * FROM entities WHERE id = %s", (entity_id,)).fetchone()
        if row:
            row = dict(row)
            row["salience"] = item.get("salience", "secondary")
            found.append(row)
            conn.execute(
                "UPDATE entities SET mentions_count = mentions_count + 1 WHERE id = %s",
                (entity_id,),
            )
    return found


def pick_for_display(conn, entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Какие карточки показать в этом посте (§7.5).

    Показываем только утверждённые, не помеченные never_explain и не
    объяснявшиеся недавно. Из оставшихся — не больше двух: primary вперёд,
    при равенстве берём менее знакомую читателю (меньше упоминаний).
    """
    s = get_settings()
    limit = int(s.get_path("entities.max_cards_per_post", 2))
    cooldown = int(s.get_path("entities.explain_cooldown_days", 35))

    ok: list[dict[str, Any]] = []
    for e in entities:
        if e["card_status"] != "approved" or e["never_explain"] or not e["card"].strip():
            continue
        if e["last_explained_at"] is not None:
            row = conn.execute(
                "SELECT EXTRACT(EPOCH FROM (now() - %s))/86400 AS d",
                (e["last_explained_at"],),
            ).fetchone()
            if float(row["d"]) < cooldown:
                continue
        ok.append(e)

    ok.sort(key=lambda e: (e.get("salience") != "primary", e["mentions_count"]))
    return ok[:limit]


# Звёздочка у имени в тексте говорит, что ниже про него есть пояснение.
# Без неё свёрнутый блок легко пропустить: он выглядит как цитата.
CONTEXT_MARK = "*"


def mark_entities(text_md: str, entities: list[dict[str, Any]]) -> str:
    """Ставит звёздочку у имён, для которых в посте есть карточка.

    Помечается только первое вхождение: звёздочка — указатель, а не сноска
    на каждое упоминание. Ищем и испанское написание, и русское: в тексте
    может встретиться любое.
    """
    if not text_md or not entities:
        return text_md

    for e in entities:
        for name in (e.get("name_es"), e.get("name_ru")):
            name = (name or "").strip()
            if not name:
                continue
            # \b не работает с кириллицей одинаково в разных позициях,
            # поэтому границу проверяем явно: не буква и не цифра
            pattern = re.compile(
                r"(?<![^\W\d_])" + re.escape(name) + r"(?![^\W\d_])"
            )
            new, n = pattern.subn(lambda m: m.group(0) + CONTEXT_MARK, text_md, count=1)
            if n:
                text_md = new
                break  # одно имя на сущность
    return text_md


def strip_leading_name(card: str, *names: str) -> str:
    """Убирает имя из начала карточки.

    Имя подставляется отдельно и ссылкой, поэтому повтор в тексте даёт
    «Margarita Robles — Margarita Robles — министр обороны». Промпт это
    запрещает, но полагаться только на промпт нельзя.
    """
    text = (card or "").strip()
    for name in names:
        name = (name or "").strip()
        if not name:
            continue
        if text.lower().startswith(name.lower()):
            rest = text[len(name):].lstrip()
            rest = rest.lstrip("—–-:,").lstrip()
            if rest:
                return rest[0].upper() + rest[1:]
    return text


def render_cards_html(entities: list[dict[str, Any]]) -> str:
    """Свёрнутая цитата. Пустого блока быть не должно — нет карточек, нет блока.

    Заголовка у блока нет намеренно: «Кто это» занимает первую строку, которая
    и так видна в свёрнутом виде, — вместо пояснения читатель видит служебную
    подпись. Пусть в свёрнутом состоянии виднеется само пояснение.
    """
    if not entities:
        return ""
    esc = html.escape
    lines = []
    for e in entities:
        name = esc(e["name_es"])
        # тексты Википедии под CC BY-SA: ссылка на статью-источник обязательна,
        # поэтому имя при показе — ссылка (§11)
        url = e.get("wiki_url_ru") or e.get("wiki_url_es")
        title = f'<a href="{esc(url, quote=True)}">{name}</a>' if url else f"<b>{name}</b>"
        card = strip_leading_name(e["card"], e.get("name_es"), e.get("name_ru"))
        lines.append(f"{title} — {esc(card)}")
    return "<blockquote expandable>" + "\n".join(lines) + "</blockquote>"


def mark_shown(conn, entities: list[dict[str, Any]], cluster_id: int | None,
               post_id: int | None, shown_ids: set[str]) -> None:
    for e in entities:
        conn.execute(
            """
            INSERT INTO entity_mentions (entity_id, cluster_id, post_id, shown)
            VALUES (%s, %s, %s, %s)
            """,
            (e["id"], cluster_id, post_id, e["id"] in shown_ids),
        )
        if e["id"] in shown_ids:
            conn.execute(
                "UPDATE entities SET last_explained_at = now() WHERE id = %s", (e["id"],)
            )


def notify_new_unresolved(conn, min_count: int | None = None) -> int:
    """Сообщает в чат ревью о новых неразрешённых сущностях.

    Одним сообщением, а не по штуке на каждую: очередь наполняется быстрее,
    чем владелец успевает её разбирать, и поштучные уведомления она бы
    превратила в шум, который перестают читать.

    Порог по частоте отсекает разовые: имя, встретившееся один раз, скорее
    всего больше не появится, и заводить под него сущность незачем.
    """
    from .telegram import notify_owner

    s = get_settings()
    threshold = min_count if min_count is not None else int(
        s.get_path("entities.notify_min_count", 2)
    )

    rows = conn.execute(
        """
        SELECT surface, count, candidate_id, sample_context, sample_urls
        FROM entity_unresolved
        WHERE notified_at IS NULL AND count >= %s
        ORDER BY count DESC, last_seen DESC
        LIMIT 15
        """,
        (threshold,),
    ).fetchall()
    if not rows:
        return 0

    lines = ["<b>Новые сущности в очереди</b>", ""]
    for r in rows:
        hint = f" — похоже на <code>{r['candidate_id']}</code>" if r["candidate_id"] else ""
        lines.append(f"• <b>{html.escape(r['surface'])}</b> ×{r['count']}{hint}")
        # ссылка на новость: по ней и понятно, кто это
        for u in (r["sample_urls"] or [])[:2]:
            lines.append(
                f'   ↳ <a href="{html.escape(u["url"], quote=True)}">'
                f'{html.escape(u["title"][:64])}</a> — {html.escape(u["source"])}'
            )
    lines += [
        "",
        "Завести:",
        "<pre>python manage.py entity add &lt;id&gt; \\\n"
        "  --name-es «Имя» --card «Кто это, до 200 символов»</pre>",
        "Или добавить алиасом к существующей:",
        "<pre>python manage.py entity alias &lt;id&gt; «строка»</pre>",
    ]
    notify_owner("\n".join(lines))

    conn.execute(
        "UPDATE entity_unresolved SET notified_at = now() WHERE surface = ANY(%s)",
        ([r["surface"] for r in rows],),
    )
    return len(rows)


# ------------------------------------------------------- замерный режим


def is_measure_day(now) -> bool:
    """Сегодня ли замерный выпуск (§10)."""
    s = get_settings()
    if not s.get_path("measure.enabled", False):
        return False
    return now.weekday() == int(s.get_path("measure.weekday", 2))


def cards_keyboard(entities: list[dict[str, Any]], post_id: int) -> dict[str, Any] | None:
    """Карточки на инлайн-кнопках вместо цитаты.

    Раскрытие цитаты никак не считается, а тап — считается. Ради этого
    и заведён замерный режим: проверить, трогают ли контекст вообще.
    """
    if not entities:
        return None
    row = []
    for e in entities[:2]:
        data = f"e:{e['id']}:{post_id}"
        if len(data.encode()) > 64:
            # ограничение Telegram; длинный slug просто не показываем кнопкой
            log.warning("callback_data длиннее 64 байт для %s — кнопка пропущена",
                        e["id"])
            continue
        row.append({"text": e["name_es"][:32], "callback_data": data})
    return {"inline_keyboard": [row]} if row else None


def handle_tap(conn, entity_id: str, post_id: int, user_id: int | None) -> str:
    """Ответ на нажатие: текст карточки всплывающим окном."""
    row = conn.execute(
        "SELECT name_es, card FROM entities WHERE id = %s", (entity_id,)
    ).fetchone()
    conn.execute(
        "INSERT INTO context_taps (entity_id, post_id, user_id) VALUES (%s,%s,%s)",
        (entity_id, post_id or None, user_id),
    )
    if row is None:
        return "Пояснение недоступно"
    # 200 символов — предел answerCallbackQuery, отсюда и лимит карточки
    return f"{row['name_es']} — {row['card']}"[:200]
