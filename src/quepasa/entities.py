"""Сущности: матчинг, правила показа, рендер блока контекста (§7).

Блок отвечает только на «кто это». Почему новость важна — это significance
в теле поста: одна и та же сущность в разных сюжетах важна по-разному.

Сам текст блока здесь не рождается: он собирается из пула проверенных фактов
под тему конкретного поста (см. `facts.py`) и хранится на посте. Здесь только
правила показа и разметка.

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
        INSERT INTO entity_unresolved
            (surface, surface_raw, candidate_id, sample_context, sample_urls)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (surface) DO UPDATE
            SET count = entity_unresolved.count + 1,
                last_seen = now(),
                candidate_id = COALESCE(entity_unresolved.candidate_id, EXCLUDED.candidate_id),
                -- исходное написание нужно с диакритикой: из «oscar puente»
                -- не собрать «Óscar Puente» ни для имени, ни для Википедии
                surface_raw = COALESCE(entity_unresolved.surface_raw, EXCLUDED.surface_raw),
                -- ссылки держим от первой встречи: она и есть повод завести
                sample_urls = CASE
                    WHEN entity_unresolved.sample_urls = '[]'::jsonb
                    THEN EXCLUDED.sample_urls ELSE entity_unresolved.sample_urls END
        """,
        (normalize(surface), (surface or "").strip(), candidate, context[:300],
         _json.dumps(urls, ensure_ascii=False)),
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
    """Кому в этом посте собирать контекст.

    Берём тех, у кого есть пул фактов, кто не помечен never_explain и кого
    не объясняли недавно. Из оставшихся — не больше двух: primary вперёд,
    при равенстве берём менее знакомую читателю (меньше упоминаний).

    Это ещё не показ: собранный контекст может не получиться (подходящих
    под тему фактов нет, сборка не прошла проверку), и тогда сущность
    в блок не попадёт. Читателя в этом случае держит role_gloss в теле поста.
    """
    from .facts import has_pool

    s = get_settings()
    limit = int(s.get_path("entities.max_cards_per_post", 2))
    cooldown = int(s.get_path("entities.explain_cooldown_days", 35))

    ok: list[dict[str, Any]] = []
    for e in entities:
        if e["never_explain"] or not has_pool(conn, e["id"]):
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
    """Убирает имя из начала собранного контекста.

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


def context_text(contexts: dict[str, Any] | None, entity_id: str) -> tuple[str, str]:
    """Собранный контекст сущности и ссылка на источник его фактов."""
    item = (contexts or {}).get(entity_id)
    if isinstance(item, str):
        return item.strip(), ""
    if isinstance(item, dict):
        return (item.get("context") or "").strip(), (item.get("url") or "").strip()
    return "", ""


def render_cards_html(entities: list[dict[str, Any]],
                      contexts: dict[str, Any] | None = None) -> str:
    """Свёрнутая цитата. Пустого блока быть не должно — нет контекста, нет блока.

    Текст берётся из собранного заранее контекста, а не из сущности: он
    зависит от темы поста, и один и тот же человек в новости про газету и
    в новости про стадион описывается разными фактами из одного пула.

    Заголовка у блока нет намеренно: «Кто это» занимает первую строку, которая
    и так видна в свёрнутом виде, — вместо пояснения читатель видит служебную
    подпись. Пусть в свёрнутом состоянии виднеется само пояснение.
    """
    if not entities:
        return ""
    esc = html.escape
    lines = []
    for e in entities:
        text, url = context_text(contexts, e["id"])
        if not text:
            continue
        name = esc(e["name_es"])
        # тексты Википедии под CC BY-SA: ссылка на источник обязательна,
        # поэтому имя при показе — ссылка (§11). Ведёт она туда, откуда взяты
        # показанные факты, а не в общую статью о сущности.
        url = url or e.get("wiki_url_ru") or e.get("wiki_url_es") or ""
        title = f'<a href="{esc(url, quote=True)}">{name}</a>' if url else f"<b>{name}</b>"
        lines.append(f"{title} — {esc(strip_leading_name(text, e.get('name_es'), e.get('name_ru')))}")
    if not lines:
        return ""
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


def _clip(text: str, limit: int) -> str:
    """Обрезка по границе слова: «critica que no impidiese» читается,
    «critica que no impidies» выглядит поломкой."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return (cut or text[:limit]) + "…"


def entity_slug(name: str) -> str:
    """Имя -> id сущности: «Óscar Puente» -> «oscar-puente»."""
    return "-".join(normalize(name).split())


def notify_new_unresolved(conn, min_count: int | None = None) -> int:
    """Предлагает завести сущность — сообщением с кнопками, по одному на имя.

    По одному, а не пачкой: у каждого имени своё решение, а решение в Telegram
    выражается кнопкой. Пачка экономит уведомления, но действовать по ней
    можно только из консоли, которой у владельца под рукой нет, — очередь
    тогда просто копится, а посты выходят с именами, не знакомыми читателю.

    Порог по частоте задаётся в конфиге; см. entities.notify_min_count.
    """
    from .telegram import notify_owner

    s = get_settings()
    threshold = min_count if min_count is not None else int(
        s.get_path("entities.notify_min_count", 2)
    )
    limit = int(s.get_path("entities.notify_max_per_run", 8))

    rows = conn.execute(
        """
        SELECT id, surface, surface_raw, count, candidate_id, sample_urls
        FROM entity_unresolved
        WHERE notified_at IS NULL AND ignored_at IS NULL AND count >= %s
        ORDER BY count DESC, last_seen DESC
        LIMIT %s
        """,
        (threshold, limit),
    ).fetchall()
    if not rows:
        return 0

    for r in rows:
        name = r["surface_raw"] or r["surface"]
        lines = [
            f"🆕 <b>{html.escape(name)}</b>",
            f"<i>Имя из вышедшего поста, карточки нет. Упоминаний: {r['count']}.</i>",
        ]
        urls = r["sample_urls"] or []
        if urls:
            lines += ["", "<b>Где встретилось:</b>"]
            for u in urls[:3]:
                lines.append(
                    f'• <a href="{html.escape(u["url"], quote=True)}">'
                    f'{html.escape(_clip(u["title"], 80))}</a> — {html.escape(u["source"])}'
                )

        keyboard = [[
            {"text": "✅ Завести", "callback_data": f"unres:add:{r['id']}"},
            {"text": "✖️ Не нужно", "callback_data": f"unres:skip:{r['id']}"},
        ]]
        # Похожее имя уже заведено — почти всегда это другое написание, и
        # правильный ответ не «завести вторую», а «дописать алиасом».
        if r["candidate_id"]:
            keyboard.insert(0, [{
                "text": f"🔗 Это {r['candidate_id']}",
                "callback_data": f"unres:alias:{r['id']}",
            }])
        notify_owner("\n".join(lines), reply_markup={"inline_keyboard": keyboard})

    conn.execute(
        "UPDATE entity_unresolved SET notified_at = now() WHERE id = ANY(%s)",
        ([r["id"] for r in rows],),
    )
    return len(rows)


def act_on_unresolved(conn, unres_id: int, action: str) -> tuple[str | None, str, str]:
    """Обрабатывает нажатие на предложении.

    Возвращает (entity_id, ответ владельцу, контекст для поиска в Википедии).
    entity_id непустой только тогда, когда для сущности надо собрать карточку.
    Контекст обязателен: «Javier Negre» без него поиск разрешает в «San Javier».
    """
    row = conn.execute(
        "SELECT * FROM entity_unresolved WHERE id = %s", (unres_id,)
    ).fetchone()
    if row is None:
        return None, "Этой строки в очереди уже нет", ""

    name = (row["surface_raw"] or row["surface"]).strip()

    if action == "skip":
        conn.execute(
            "UPDATE entity_unresolved SET ignored_at = now() WHERE id = %s", (unres_id,)
        )
        return None, f"{name}: больше не предлагаю", ""

    if action == "alias":
        target = row["candidate_id"]
        if not target:
            return None, "Не к чему привязывать", ""
        conn.execute(
            "INSERT INTO entity_aliases (entity_id, alias) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING", (target, row["surface"]),
        )
        conn.execute("DELETE FROM entity_unresolved WHERE id = %s", (unres_id,))
        return None, f"{name} → {target}", ""

    entity_id = entity_slug(name)
    if not entity_id:
        return None, "Из этого имени не выходит идентификатор", ""

    # заголовок новости — лучший уточнитель, какой у нас есть: он про то же
    # событие, в котором имя встретилось
    titles = [u.get("title", "") for u in (row["sample_urls"] or [])]
    context = (row.get("sample_context") or " ".join(titles))[:300]

    # Сущность с таким id уже есть — значит, не сматчилось написание, а не
    # появился новый человек. Вторая запись развела бы факты по двум карточкам.
    exists = conn.execute(
        "SELECT id FROM entities WHERE id = %s", (entity_id,)
    ).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO entities (id, name_es, type) VALUES (%s, %s, 'other')",
            (entity_id, name),
        )

    conn.execute(
        "INSERT INTO entity_aliases (entity_id, alias) VALUES (%s, %s) "
        "ON CONFLICT DO NOTHING", (entity_id, row["surface"]),
    )
    conn.execute("DELETE FROM entity_unresolved WHERE id = %s", (unres_id,))

    if exists:
        return None, f"{name}: добавлено написанием к {entity_id}", ""
    return entity_id, f"{name}: собираю карточку…", context


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
    """Ответ на нажатие: собранный для этого поста контекст всплывающим окном.

    Текст берём из самого поста, а не из сущности: он собирался под эту тему,
    и показать надо ровно то, что показала бы цитата в обычном выпуске.
    """
    row = conn.execute(
        "SELECT name_es FROM entities WHERE id = %s", (entity_id,)
    ).fetchone()
    post = conn.execute(
        "SELECT entity_context FROM posts WHERE id = %s", (post_id or 0,)
    ).fetchone()
    conn.execute(
        "INSERT INTO context_taps (entity_id, post_id, user_id) VALUES (%s,%s,%s)",
        (entity_id, post_id or None, user_id),
    )
    if row is None:
        return "Пояснение недоступно"
    text, _ = context_text(post["entity_context"] if post else None, entity_id)
    if not text:
        return "Пояснение недоступно"
    # 200 символов — предел answerCallbackQuery, отсюда и лимит сборки
    return f"{row['name_es']} — {text}"[:200]
