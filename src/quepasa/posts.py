"""Пост на сюжет: сборка, публикация, дополнение по мере выхода изданий.

Устройство поста:

    [ шапка — пишет человек, markdown ]

    [ блок ссылок — собирается заново при каждой правке ]

    [ хэштеги: тема и место ]

Шапка и блок ссылок разделены намеренно. Издания публикуют не одновременно,
поэтому пост дополняется: подтянулось ещё одно издание — правим сообщение,
а не постим второе. При этом ручной текст трогать нельзя, поэтому пересобирается
только блок ссылок.
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from typing import Any, Iterable

from .config import env, get_settings
from .db import connect
from .markup import markdown_to_telegram_html
from .entities import (
    mark_shown, notify_new_unresolved, pick_for_display, resolve_all,
)
from .facts import contexts_for
from .llm import LLMUsage
from .postgate import check_post
from .related import find_related
from .telegram import edit_message_text, notify_owner, send_message

log = logging.getLogger(__name__)

# Порядок — это порядок спектра, слева направо. Он же порядок блоков в посте.
LEAN_ORDER = [
    "far-left", "left", "center-left", "center", "center-right", "right", "far-right",
]

# Позиция в спектре обозначается стрелкой, а не словом: короче, читается с одного
# взгляда и не занимает половину строки на узком экране телефона.
LEAN_EMOJI = {
    "far-left": "⏪",
    "left": "⬅️",
    "center-left": "◀️",
    "center": "↔️",
    "center-right": "▶️",
    "right": "➡️",
    "far-right": "⏩",
}

# Официальные источники — не позиция в спектре, а первоисточник.
OFFICIAL_EMOJI = "🏛"
# Агентство подтверждает, что событие произошло, но политической координаты
# не имеет. Показывать его центристской стрелкой — врать читателю.
AGENCY_EMOJI = "📰"

# Расшифровка для закреплённого поста канала: без неё стрелки читателю ни о чём
# не говорят. Печатается командой `python -m quepasa.posts --legend`.
LEGEND = [
    ("⏪", "крайне левые"),
    ("⬅️", "левые"),
    ("◀️", "левоцентристские"),
    ("↔️", "центристские"),
    ("▶️", "правоцентристские"),
    ("➡️", "правые"),
    ("⏩", "крайне правые"),
    ("📰", "агентства — без политической позиции"),
    ("🏛", "официальные источники"),
]

# §2: world не получает отдельного поста никогда — только строку в «Коротко».
SCOPES = ("spain", "region", "hispanic", "world_linked", "world")
NO_POST_SCOPES = ("world",)

TOPIC_HASHTAG = {
    "политика": "политика",
    "экономика": "экономика",
    "общество": "общество",
    "эмиграция": "эмиграция",
    "происшествия": "происшествия",
    "культура/спорт": "культура_и_спорт",
}


# Обороты, с которых начинается пересказ заголовка вместо практического
# последствия. Промпт это запрещает, но запрет в промпте — просьба, а не
# гарантия: за неделю в канал ушло десять строк вида «Касается жителей
# Сеуты, обеспокоенных безопасностью». Читатель это и так понял из заголовка.
SIGNIFICANCE_JUNK = [
    re.compile(r"^\s*кас[ае]ется\b", re.I),
    re.compile(r"^\s*затрагивает\b", re.I),
    re.compile(r"^\s*важно для\b", re.I),
    # Эти — не только с начала строки: «Решение отражает позицию правительства»
    # ровно то же самое, просто с подлежащим впереди.
    re.compile(r"\bотража[ею]т\b", re.I),
    re.compile(r"\bсвидетельству[ею]т\b", re.I),
    # А эти оставляем с якоря: «Правительство подтверждает выплаты до декабря»
    # и «Таблица показывает сроки» — сообщают факт, а не пустоту.
    re.compile(r"^\s*показыва[ею]т\b", re.I),
    re.compile(r"^\s*подтвержда[ею]т\b", re.I),
    re.compile(r"^\s*подтверждение\b", re.I),
    re.compile(r"^\s*демонстрир[ую]", re.I),
    # не с начала строки: «Случай привлёк внимание к…» — тот же пустой оборот,
    # просто с подлежащим впереди. И «ё» пишут не всегда
    re.compile(r"привл[её]к\w*\s+внимание\b", re.I),
    re.compile(r"\bподн[ияа]\w*\s+вопрос\b", re.I),
    re.compile(r"^\s*(станов|ста[её]т|стал\w*)\s+(поводом|предметом|темой)\b", re.I),
    re.compile(r"^\s*знаков\w*\s+(событие|момент)\b", re.I),
    re.compile(r"^\s*может (привести|повлиять|стать)\b", re.I),
    re.compile(r"^\s*разворачивается\b", re.I),
]


# Испанские названия, которые модель то переводит, то записывает кириллицей.
# Промпт это запрещает прямо, и всё равно в канал ушло «Автономные сообщества
# ПП»: запрет на перевод модель обошла транслитерацией аббревиатуры. Две
# кириллические буквы не говорят читателю ничего — ни перевода, ни оригинала.
#
# Порядок важен: длинные формы идут первыми, иначе «Народная партия»
# успеет развалиться на части.
NAME_FIXES = [
    (re.compile(r"\bИспанск\w+\s+социалистическ\w+\s+рабоч\w+\s+парти\w+", re.I), "PSOE"),
    (re.compile(r"\bНародн\w+\s+парти\w+", re.I), "PP"),
    (re.compile(r"\bПСОЭ\b"), "PSOE"),
    (re.compile(r"\bПП\b"), "PP"),
    (re.compile(r"\bВ[оО]кс\b|\bВОКС\b"), "Vox"),
    (re.compile(r"\bПодемос\b", re.I), "Podemos"),
    (re.compile(r"\bСумар\b", re.I), "Sumar"),
    (re.compile(r"\b[ЖХ]унтс\b", re.I), "Junts"),
    (re.compile(r"\bСьюдаданос\b", re.I), "Ciudadanos"),
    (re.compile(r"\bЭрк\b|\bЭРК\b"), "ERC"),
]


# Приблизительная испанская транскрипция: нужна не для показа, а для
# сравнения. «Leire Díez» -> «лейре диес», и кириллическое «Лейре Диез»
# из заголовка отличается от неё на одну букву.
_TRANSLIT = [
    ("ch", "ч"), ("ll", "й"), ("qu", "к"), ("gu", "г"), ("rr", "р"),
    ("ñ", "нь"), ("á", "а"), ("é", "е"), ("í", "и"), ("ó", "о"), ("ú", "у"),
    ("ü", "у"), ("à", "а"), ("è", "е"), ("ò", "о"), ("ç", "с"),
    ("j", "х"), ("h", ""), ("z", "с"), ("v", "в"), ("y", "и"), ("x", "кс"),
    ("w", "в"), ("c", "к"), ("q", "к"), ("a", "а"), ("b", "б"), ("d", "д"),
    ("e", "е"), ("f", "ф"), ("g", "г"), ("i", "и"), ("k", "к"), ("l", "л"),
    ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("r", "р"), ("s", "с"),
    ("t", "т"), ("u", "у"),
]

_LATIN_NAME = re.compile(r"\b[A-ZÁÉÍÓÚÑÜÀÈÒÇ][\wÁÉÍÓÚÑÜáéíóúñüàèòç'’-]+")
_CYRILLIC_WORD = re.compile(r"[А-ЯЁ][а-яё]+")

# Слова, с которых начинается предложение в испанском заголовке: сами по себе
# именами не являются и в кандидаты попадать не должны.
_NOT_NAMES = {
    "El", "La", "Los", "Las", "Un", "Una", "De", "Del", "En", "Por", "Para",
    "Con", "Sin", "Sobre", "Tras", "Ante", "Hasta", "Desde", "Que", "Se",
    "Su", "Sus", "Al", "Y", "O", "No", "Es", "Ha", "Han", "Más", "Este",
    "Esta", "Estos", "Estas", "Todo", "Toda", "Todos", "Todas", "Primer",
    "Primera", "Nuevo", "Nueva", "Gobierno", "España", "Madrid", "Barcelona",
}


def _to_cyrillic(name: str) -> str:
    out = name.casefold()
    for latin, cyr in _TRANSLIT:
        out = out.replace(latin, cyr)
    return re.sub(r"[^а-яё ]", "", out).strip()


def _latin_names_in(titles: Iterable[str]) -> list[str]:
    """Имена собственные из заголовков источников: одно-три слова подряд."""
    found: list[str] = []
    for title in titles:
        words = _LATIN_NAME.findall(title or "")
        run: list[str] = []
        for w in words:
            if w in _NOT_NAMES:
                run = []
                continue
            run.append(w)
        # склеиваем подряд идущие: «Leire Díez», «Pedro Sánchez»
        for m in re.finditer(
            r"(?:[A-ZÁÉÍÓÚÑÜ][\wÁÉÍÓÚÑÜáéíóúñüàèòç'’-]+)"
            r"(?:\s+(?:de|del|la|y)?\s*[A-ZÁÉÍÓÚÑÜ][\wÁÉÍÓÚÑÜáéíóúñüàèòç'’-]+){0,2}",
            title or "",
        ):
            phrase = m.group(0).strip()
            parts = phrase.split()
            while parts and parts[0] in _NOT_NAMES:
                parts.pop(0)
            if len(parts) >= 2:
                found.append(" ".join(parts))
    return found


def _is_place(name: str, places: set[str]) -> bool:
    """Место или нет. Проверяем и латиницу, и её кириллическую форму.

    В словаре гео-тегов у «#Мадрид» перечислены «Мадрид» и «Comunidad de
    Madrid», но не голое «Madrid». Транскрипция закрывает это без правки
    словаря: «Madrid» -> «мадрид», и совпадение находится.
    """
    from .geo import _norm

    return _norm(name) in places or _to_cyrillic(name) in places


def restore_latin_names(text: str, titles: Iterable[str]) -> str:
    """Возвращает транскрибированным именам латиницу из источников.

    Правило «никакой транскрипции» есть в промпте, и всё равно в канал ушли
    «Лейре Диез», «Кристиану Роналду» и «Араухо». Здесь оно чинится: у нас
    на руках заголовки источников, где то же имя написано латиницей, —
    остаётся сопоставить их с кириллицей в нашем тексте.

    Сравнение приблизительное: транскрипция у моделей разная («Роналду» и
    «Роналдо»), поэтому допускаем пару опечаток на имя.
    """
    from .entities import _levenshtein

    out = text or ""
    if not out:
        return out

    # Кроме полного имени пробуем одну фамилию: в заголовке часто остаётся
    # только она — «Араухо покинул Barcelona».
    # География остаётся русской, поэтому места исключаем — и по полному
    # названию, и по последнему слову: «Sierra de Madrid» дала бы «Madrid»
    # в обход списка и переписала бы «в горах Мадрида».
    from .geo import place_names

    places = place_names()

    variants: list[str] = []
    for name in dict.fromkeys(_latin_names_in(titles)):
        if _is_place(name, places):
            continue
        variants.append(name)
        surname = name.split()[-1]
        # короткие слова не берём: «Vox» или «Ruiz» слишком легко совпадают
        if len(surname) >= 6 and not _is_place(surname, places):
            variants.append(surname)

    for name in dict.fromkeys(variants):
        if name.casefold() in out.casefold():
            continue  # уже латиницей — трогать нечего
        target = _to_cyrillic(name)
        if not target:
            continue
        n_words = len(name.split())
        words = _CYRILLIC_WORD.findall(out)
        for i in range(len(words) - n_words + 1):
            window = words[i:i + n_words]
            got = " ".join(window).casefold()
            # порог: имя длиной 10 букв переживает две замены, короткое — одну
            limit = 2 if len(target) >= 8 else 1
            if _levenshtein(got, target, cap=limit + 1) <= limit:
                phrase = r"\s+".join(re.escape(w) for w in window)
                out = re.sub(phrase, name, out)
                log.info("Имя восстановлено латиницей: %s -> %s",
                         " ".join(window), name)
                break
    return out


def fix_names(text: str) -> str:
    """Возвращает испанские названия в испанское написание.

    Правило есть в промпте, но промпт — просьба: модель обходит запрет на
    перевод транслитерацией, а запрет на транслитерацию — переводом.
    """
    out = text or ""
    for pattern, correct in NAME_FIXES:
        fixed = pattern.sub(correct, out)
        if fixed != out:
            log.info("Название приведено к испанскому написанию: %s", correct)
            out = fixed
    return out


def clean_significance(text: str) -> str:
    """Пустая строка вместо пересказа заголовка.

    Проверка у промпта одна: узнал ли читатель что-то, чего не было
    в заголовке. Здесь ловятся типовые обороты, которыми модель обходит
    это правило, когда сказать нечего.
    """
    value = (text or "").strip()
    if not value:
        return ""
    if any(p.search(value) for p in SIGNIFICANCE_JUNK):
        log.info("significance отброшен как пересказ: %s", value[:80])
        return ""
    return value


def clean_entities(raw: Any) -> list[dict[str, Any]]:
    """Приводит список сущностей от модели к виду, пригодному для показа.

    Главное здесь — `role_gloss`: это единственный слой контекста, который
    покрывает 100% сущностей, и он же единственный, который модель пишет
    свободным текстом. Поэтому его чистим строже прочего: должность и роль
    разрешены, характеристика — нет. «Скандальный журналист Javier Negre»
    в теле поста звучит от имени канала, и никакая цитата это не спасает.
    """
    from .wordlists import hits, load

    max_words = int(get_settings().get_path("entities.role_gloss_max_words", 6))
    blocked = load("blocklist.txt")

    out: list[dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        surface = (item.get("surface") or "").strip()
        if not surface:
            continue

        gloss = " ".join((item.get("role_gloss") or "").split()).strip(" .,;:—–-")
        if gloss:
            bad = hits(gloss, blocked)
            if bad:
                log.info("role_gloss «%s» отброшен: оценочное «%s»", gloss, bad[0])
                gloss = ""
            elif len(gloss.split()) > max_words:
                # длинная роль — это уже справка, а ей место в пуле фактов
                log.info("role_gloss «%s» отброшен: %s слов при пределе %s",
                         gloss, len(gloss.split()), max_words)
                gloss = ""

        salience = (item.get("salience") or "secondary").strip().lower()
        out.append({
            "surface": surface,
            "type": (item.get("type") or "other").strip().lower(),
            "salience": salience if salience in ("primary", "secondary") else "secondary",
            "role_gloss": gloss,
            "name_ru": (item.get("name_ru") or "").strip(),
        })
    return out


def hashtag(category: str) -> str:
    slug = TOPIC_HASHTAG.get((category or "").strip().lower())
    if slug:
        return f"#{slug}"
    # незнакомая категория: чистим до пригодного для хэштега вида
    cleaned = "".join(
        ch if ch.isalnum() or ch == "_" else "_" for ch in (category or "").strip().lower()
    ).strip("_")
    return f"#{cleaned}" if cleaned else ""


def cluster_articles(conn, cluster_id: int) -> list[dict[str, Any]]:
    """По одному материалу с издания — свежайший, с текстом в приоритете."""
    return conn.execute(
        """
        SELECT DISTINCT ON (a.source_id)
               a.id, a.title, a.url, a.url_canonical, a.published_at,
               a.source_id, s.name AS source_name, s.lean, s.type,
               -- без owner_group owner_of() молча откатывается на source_id,
               -- и правило «считаем владельцев, а не издания» перестаёт
               -- действовать: две газеты одного холдинга дают две ссылки
               s.owner_group
        FROM articles a
        JOIN sources s ON s.id = a.source_id
        WHERE a.cluster_id = %s
        ORDER BY a.source_id, (a.body IS NOT NULL) DESC, a.published_at DESC
        """,
        (cluster_id,),
    ).fetchall()


def pick_links(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Не больше MAX_LINKS_PER_POST ссылок, по одной на владельца.

    Три газеты одного холдинга — одна ссылка: они подтверждают одно и то же.
    При переполнении добираем так, чтобы покрыть как можно больше бакетов:
    строка источников должна показывать охват спектра, а не длину списка.
    """
    from .config import get_settings
    from .spectrum import bucket, lean_value, owner_of

    # 0 — без потолка: показываем всех, кто написал. Список и так ограничен
    # числом владельцев, а не изданий, и длиннее полутора десятков не бывает.
    limit = int(get_settings().get_path("autopost.max_links_per_post", 5))

    usable = [a for a in articles if a.get("url") or a.get("url_canonical")]
    by_owner: dict[str, dict[str, Any]] = {}
    for art in usable:
        # от владельца берём один материал — свежайший
        key = owner_of(art)
        cur = by_owner.get(key)
        if cur is None or art["published_at"] > cur["published_at"]:
            by_owner[key] = art

    pool = sorted(
        by_owner.values(),
        key=lambda a: (lean_value(a["lean"]) if a.get("type") != "official" else 99) or 0,
    )
    if limit <= 0 or len(pool) <= limit:
        return pool

    # Раскладываем по бакетам и берём по кругу. Простой добор «сверху вниз»
    # смещает выборку влево: пул отсортирован по шкале, и лишние места
    # достаются левому краю, а правый в строку не попадает вовсе.
    groups: dict[str, list[dict[str, Any]]] = {}
    for art in pool:
        if art.get("type") == "agency":
            key = "agency"
        elif art.get("type") == "official":
            key = "official"
        else:
            key = bucket(art["lean"]) or "center"
        groups.setdefault(key, []).append(art)

    # Внутри бакета — сначала самые крайние: строка источников должна
    # показывать максимальный охват. Иначе «правое» представит
    # правоцентристское издание, а настоящий правый фланг не попадёт в пост.
    for key, group in groups.items():
        if key in ("left", "right"):
            group.sort(key=lambda a: -abs(lean_value(a["lean"]) or 0))

    order = ["left", "right", "center", "agency", "official"]
    picked: list[dict[str, Any]] = []
    while len(picked) < limit and any(groups.get(k) for k in order):
        for key in order:
            if len(picked) >= limit:
                break
            if groups.get(key):
                picked.append(groups[key].pop(0))
    return sorted(
        picked[:limit],
        key=lambda a: (lean_value(a["lean"]) if a.get("type") != "official" else 99) or 0,
    )


def group_sources_md(articles: list[dict[str, Any]]) -> str:
    """Издания одной строкой: значок полюса, за ним издания этого полюса.

        ⬅️ elDiario.es · infoLibre · ◀️ 20minutos · ➡️ La Razón

    Значок ставится один раз на группу, а не на каждое издание: две газеты
    одного фланга — это по-прежнему один фланг, и повтор стрелки только
    удлиняет строку. Разделитель везде один и тот же, границу группы
    показывает сам значок.

    Порядок — слева направо по шкале, потом агентства, потом официальные
    источники: у них позиции нет, и в спектр они не встают.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for art in articles:
        if not (art.get("url") or art.get("url_canonical")):
            continue
        if art.get("type") == "official":
            key = "official"
        elif art.get("type") == "agency":
            key = "agency"
        else:
            key = art["lean"]
        groups.setdefault(key, []).append(art)

    parts: list[str] = []
    for key in [*LEAN_ORDER, "agency", "official"]:
        group = groups.get(key)
        if not group:
            continue
        marker = {"agency": AGENCY_EMOJI, "official": OFFICIAL_EMOJI}.get(
            key, LEAN_EMOJI.get(key, "")
        )
        links = " · ".join(
            f'[{a["source_name"]}]({a.get("url") or a["url_canonical"]})'
            for a in sorted(group, key=lambda x: x["source_name"])
        )
        parts.append(f"{marker} {links}")
    return " · ".join(parts)


def render_links_md(articles: list[dict[str, Any]]) -> str:
    """Блок источников поста: одной строкой, сгруппированной по полюсам."""
    return group_sources_md(pick_links(articles))


def legend_md() -> str:
    """Расшифровка стрелок — для закреплённого поста канала."""
    rows = "\n".join(f"{emoji} — {name}" for emoji, name in LEGEND)
    return (
        "**Как читать значки**\n\n"
        f"{rows}\n\n"
        "Значок показывает политическую позицию издания, а не оценку новости. "
        "Позиции проставлены вручную."
    )


# Строка про односторонность. Ветка «≥N владельцев» пропускает синхронные
# кампании одного лагеря; запрещать их не за что, но читателю показывают,
# что подтверждения с другой стороны пока нет.
ONE_SIDED_LINE = "_Пока пишут только издания одного лагеря._"


def hashtags(category: str, geo_tag: str | None = None) -> str:
    """Хэштеги поста: тема и место.

    Гео-тег ставится не всегда: национальная новость его не получает, потому
    что #Испания в канале про Испанию ничего не сужает. Порядок — тема, потом
    место: по теме подписываются, по месту ищут.
    """
    parts = [t for t in (hashtag(category), (geo_tag or "").strip()) if t]
    return " ".join(parts)


def compose_md(header_md: str, articles: list[dict[str, Any]], category: str,
               *, one_sided: bool = False, significance: str = "",
               geo_tag: str | None = None) -> str:
    """Полный текст поста: шапка, зачем это читателю, оговорка, ссылки, хэштеги."""
    blocks = [(header_md or "").strip()]
    if significance.strip():
        blocks.append(f"_{significance.strip()}_")
    if one_sided:
        blocks.append(ONE_SIDED_LINE)
    links = render_links_md(articles)
    if links:
        blocks.append(links)
    tags = hashtags(category, geo_tag)
    if tags:
        blocks.append(tags)
    return "\n\n".join(b for b in blocks if b)


def compose_html(header_md: str, articles: list[dict[str, Any]], category: str,
                 *, one_sided: bool = False, significance: str = "",
                 cards_html: str = "", cards: list[dict[str, Any]] | None = None,
                 related_md: str = "", geo_tag: str | None = None) -> str:
    """Полный HTML поста.

    Карточки собираются отдельным блоком, а не через markdown: blockquote
    в markdown нет, а экранировать его вручную опаснее, чем собрать готовым.
    """
    from .entities import mark_entities

    head = compose_md("", [], "", one_sided=one_sided, significance=significance)
    head = "\n\n".join(x for x in [(header_md or "").strip(), head] if x)
    # звёздочка у имени показывает, что про него есть пояснение ниже
    head = mark_entities(head, cards or [])
    # Два вида связности в одном посте превращают его в оглавление, поэтому
    # «Ранее по теме» не ставится, если пост и так уходит реплаем (§4.2).
    tail_parts = [related_md, render_links_md(articles), hashtags(category, geo_tag)]
    tail = "\n\n".join(x for x in tail_parts if x)

    blocks = [markdown_to_telegram_html(head)]
    if cards_html:
        blocks.append(cards_html)
    if tail:
        blocks.append(markdown_to_telegram_html(tail))
    return "\n\n".join(blocks)


# ------------------------------------------------------------------ хранилище


def get_post(conn, cluster_id: int) -> dict[str, Any] | None:
    """Черновик, если есть, иначе последний опубликованный пост сюжета."""
    return conn.execute(
        """
        SELECT * FROM posts WHERE cluster_id = %s
        ORDER BY (status = 'draft') DESC, id DESC LIMIT 1
        """,
        (cluster_id,),
    ).fetchone()


def upsert_draft(conn, cluster_id: int, header_md: str, category: str,
                 *, significance: str = "", one_sided: bool = False,
                 scope: str | None = None, geo_tag: str | None = None,
                 is_continuation: bool = False) -> dict[str, Any]:
    """Черновик поста. Продолжение — новая строка, а не правка старой:
    предыдущий пост остаётся на месте, и на него будет реплай."""
    draft = conn.execute(
        "SELECT * FROM posts WHERE cluster_id = %s AND status = 'draft' "
        "ORDER BY id DESC LIMIT 1",
        (cluster_id,),
    ).fetchone()

    if draft is not None:
        return conn.execute(
            """
            UPDATE posts SET header_md=%s, category=%s, significance=%s,
                   one_sided=%s, scope=%s, geo_tag=%s, is_continuation=%s
            WHERE id = %s RETURNING *
            """,
            (header_md, category, significance, one_sided, scope, geo_tag,
             is_continuation, draft["id"]),
        ).fetchone()

    return conn.execute(
        """
        INSERT INTO posts (cluster_id, header_md, category, significance,
                           one_sided, scope, geo_tag, is_continuation)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """,
        (cluster_id, header_md, category, significance, one_sided, scope,
         geo_tag, is_continuation),
    ).fetchone()


def default_header_md(articles: list[dict[str, Any]]) -> str:
    """Заготовка, когда пересказа от модели нет.

    Не выдаём испанский заголовок за перевод: кладём его как подсказку, чтобы
    человек написал русский заголовок сам.
    """
    if not articles:
        return ""
    newest = max(articles, key=lambda a: a["published_at"])
    return f"**{newest['title']}**"


def generate_header(cluster_id: int) -> tuple[str, str, dict]:
    """Просит модель сформулировать событие по-русски.

    Возвращает (markdown шапки, категория, метаданные). В метаданных, помимо
    стоимости, — scope, geo_tag и significance: их определяет тот же вызов,
    отдельного обращения к модели для этого не делаем.
    """
    from .config import load_prompt
    from .llm import LLMUsage, extract_json
    from .llm import _PROVIDERS  # noqa: PLC2701 — один и тот же реестр провайдеров
    from .config import get_settings

    with connect() as conn:
        articles = cluster_articles(conn, cluster_id)
    if not articles:
        raise ValueError(f"в сюжете {cluster_id} нет статей")

    lines = [
        f"- [{a['source_name']}] {a['title']}"
        for a in sorted(articles, key=lambda x: x["source_name"])
    ]
    user = "Заголовки об одном событии:\n" + "\n".join(lines)

    provider = get_settings().require("summarize.provider")
    fn = _PROVIDERS.get(provider)
    if fn is None:
        raise ValueError(f"неизвестный провайдер LLM: {provider}")

    usage = LLMUsage()
    data = extract_json(fn(load_prompt("post_headline.md"), user, usage))

    from .geo import resolve as resolve_geo

    headline = (data.get("headline") or "").strip()
    lead = (data.get("lead") or "").strip()
    topic = (data.get("topic") or "").strip().lower()
    if topic not in TOPIC_HASHTAG:
        topic = ""

    scope = (data.get("scope") or "").strip().lower()
    if scope not in SCOPES:
        scope = ""
    # тег вне словаря отбрасывается здесь, а не доезжает до ворот
    geo_tag = resolve_geo(data.get("geo_tag"))
    if scope == "spain":
        # национальная новость гео-тега не получает: он ничего не сужает
        geo_tag = None

    # Латиница из заголовков источников важнее того, что придумала модель:
    # правило «никакой транскрипции» она нарушает регулярно.
    titles = [a["title"] for a in articles]
    headline = restore_latin_names(fix_names(headline), titles)
    lead = restore_latin_names(fix_names(lead), titles)
    header_md = f"**{headline}**" + (f"\n\n{lead}" if lead else "")

    entities = clean_entities(data.get("entities"))
    return header_md, topic, {
        "provider": provider,
        "tokens_in": usage.tokens_in,
        "tokens_out": usage.tokens_out,
        "cost_usd": round(usage.cost_usd, 4),
        "headline": headline,
        "lead": lead,
        "significance": fix_names(clean_significance(data.get("significance"))),
        "scope": scope,
        "geo_tag": geo_tag,
        "entities": entities,
    }


# ------------------------------------------------------------------ публикация


def sound_quota_left(conn) -> int:
    """Сколько громких постов ещё можно сегодня."""
    from .config import get_settings

    cap = int(get_settings().get_path("autopost.sound.max_per_day", 2))
    used = conn.execute(
        "SELECT count(*) AS n FROM posts WHERE with_sound "
        "AND published_at >= date_trunc('day', now())"
    ).fetchone()["n"]
    return max(0, cap - int(used))


def score_percentile(conn, pct: float, days: int) -> float | None:
    """Перцентиль скора опубликованных постов за период.

    None, если истории мало: на трёх постах перцентиль — это шум, и порог
    по нему был бы случайным.
    """
    row = conn.execute(
        """
        SELECT count(*) AS n,
               percentile_cont(%s) WITHIN GROUP (ORDER BY c.score) AS p
        FROM posts pt JOIN clusters c ON c.id = pt.cluster_id
        WHERE pt.status = 'published'
          AND pt.published_at >= now() - make_interval(days => %s)
        """,
        (pct, days),
    ).fetchone()
    min_n = int(get_settings().get_path("autopost.sound.percentile_min_posts", 20))
    if not row or int(row["n"]) < min_n or row["p"] is None:
        return None
    return float(row["p"])


def deserves_sound(conn, row: dict[str, Any]) -> bool:
    """Громким делаем только по-настоящему крупный сюжет, и не больше квоты.

    Основной критерий — скор выше p95 за последние 30 дней: он подстраивается
    под поток сам и не требует ручной подкрутки. Пока истории мало, работает
    запасной порог по числу изданий.
    """
    s = get_settings()
    if not s.get_path("autopost.sound.enabled", True):
        return False
    if sound_quota_left(conn) <= 0:
        return False

    threshold = score_percentile(
        conn,
        float(s.get_path("autopost.sound.percentile", 0.95)),
        int(s.get_path("autopost.sound.percentile_days", 30)),
    )
    if threshold is not None:
        return float(row.get("score", 0)) >= threshold
    return int(row.get("n_sources", 0)) >= int(
        s.get_path("autopost.sound.min_sources", 5))


def publish(cluster_id: int, dry_run: bool = True, silent: bool = True,
            cards: list[dict[str, Any]] | None = None,
            related: dict[str, Any] | None = None) -> dict[str, Any]:
    """Публикует пост в канал и запоминает message_id для будущих правок.

    По умолчанию беззвучно: два десятка уведомлений в сутки — это отписки.
    """
    from .config import env

    with connect() as conn:
        post = get_post(conn, cluster_id)
        if post is None:
            raise ValueError(f"черновика для сюжета {cluster_id} нет")
        articles = cluster_articles(conn, cluster_id)

    from .entities import render_cards_html

    from datetime import datetime
    from zoneinfo import ZoneInfo

    from .entities import cards_keyboard, is_measure_day
    from .related import render_link_md

    # В замерный день пояснения уходят кнопками, а не цитатой: раскрытие
    # цитаты никак не считается, а тап — считается (§10).
    measure = is_measure_day(datetime.now(ZoneInfo(
        get_settings().require("render.timezone"))))
    related_md = render_link_md(related) if related else ""
    contexts = post.get("entity_context") or {}
    text = compose_html(
        post["header_md"], articles, post["category"],
        one_sided=bool(post.get("one_sided")),
        significance=post.get("significance") or "",
        cards_html="" if measure else render_cards_html(cards or [], contexts),
        cards=cards or [],
        related_md=related_md,
        geo_tag=post.get("geo_tag"),
    )
    if dry_run:
        log.info("DRY-RUN, пост не отправлен:\n%s", text)
        return {"status": "dry-run", "html": text}

    channel = env("TELEGRAM_CHANNEL_ID", required=True)

    # Продолжение уходит реплаем на предыдущий пост этого сюжета: читатель
    # видит, к чему это продолжение, прямо над текстом. Цепочка строится
    # «на предыдущий», а не «на корневой» — цитата должна показывать
    # последнее событие, а не завязку (§4.1).
    reply_to = None
    if post.get("is_continuation"):
        with connect() as conn:
            row = conn.execute(
                "SELECT last_post_message_id FROM clusters WHERE id = %s",
                (cluster_id,),
            ).fetchone()
        reply_to = row["last_post_message_id"] if row else None

    keyboard = cards_keyboard(cards or [], post["id"]) if measure else None
    result = send_message(channel, text, silent=silent,
                          reply_to_message_id=reply_to, reply_markup=keyboard)
    message_id = result.get("message_id")

    from .spectrum import owner_of as _owner
    source_ids = sorted({_owner(a) for a in articles})
    with connect() as conn:
        conn.execute(
            """
            UPDATE posts SET status = 'published', message_id = %s,
                   published_at = now(), posted_source_ids = %s,
                   with_sound = %s, n_articles_at_publish = %s,
                   reply_to_message_id = %s, related_md = %s
            WHERE id = %s
            """,
            (message_id, json.dumps(source_ids), not silent, len(articles),
             reply_to, related_md, post["id"]),
        )
        # цепочка ведётся по кластеру: следующий пост ответит на этот
        conn.execute(
            "UPDATE clusters SET last_post_message_id = %s WHERE id = %s",
            (message_id, cluster_id),
        )
        record_sources(conn, post["id"], articles)
        conn.execute(
            """
            UPDATE clusters SET last_published_at = now(), n_articles_at_publish = n_articles
            WHERE id = %s
            """,
            (cluster_id,),
        )

    log.info("Сюжет %s опубликован%s%s, message_id=%s",
             cluster_id, "" if silent else " СО ЗВУКОМ",
             f" реплаем на {reply_to}" if reply_to else "", message_id)
    return {"status": "published", "message_id": message_id, "sources": source_ids,
            "silent": silent, "reply_to": reply_to}


def pending_updates(conn) -> list[dict[str, Any]]:
    """Опубликованные посты в окне правки, где что-то могло измениться.

    Кандидатов отбираем широко: точное решение принимает sync_post, он же
    знает про владельцев и про снятие пометки об односторонности.
    """
    from .config import get_settings

    window = int(get_settings().get_path("autopost.link_window_hours", 24))
    return conn.execute(
        """
        SELECT p.cluster_id, p.message_id
        FROM posts p
        WHERE p.status = 'published' AND p.message_id IS NOT NULL
          AND p.published_at >= now() - make_interval(hours => %s)
        """,
        (window,),
    ).fetchall()


def record_sources(conn, post_id: int, articles: list[dict[str, Any]]) -> None:
    """Запоминает, что прилинковано в посте."""
    from .spectrum import bucket, owner_of

    for a in articles:
        conn.execute(
            """
            INSERT INTO post_sources (post_id, article_id, owner_group, bucket)
            VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING
            """,
            (post_id, a["id"], owner_of(a),
             "official" if a.get("type") == "official" else (bucket(a["lean"]) or "")),
        )


def sync_post(cluster_id: int, dry_run: bool = True) -> dict[str, Any]:
    """Дополняет опубликованный пост новыми изданиями.

    Шапку не трогаем — это ручной текст. Пересобирается блок ссылок и строка
    об односторонности: сюжет, вышедший с пометкой «пишут только издания
    одного лагеря», при появлении источника с другой стороны теряет её
    автоматически. Обратное движение невозможно: пометка снимается, но
    задним числом не ставится.
    """
    from datetime import datetime, timezone

    from .config import env, get_settings
    from .spectrum import is_one_sided, owner_of

    with connect() as conn:
        post = get_post(conn, cluster_id)
        if post is None or post["status"] != "published":
            return {"status": "skip", "reason": "пост не опубликован"}
        articles = cluster_articles(conn, cluster_id)

    # Окно правки: сутки. Дальше пост замораживается — правка суточной
    # давности читателю ничего не даёт, а расход API и риск задеть текст
    # остаются.
    window = float(get_settings().get_path("autopost.link_window_hours", 24))
    if post["published_at"] is not None:
        age = (datetime.now(timezone.utc) - post["published_at"]).total_seconds() / 3600
        if age > window:
            return {"status": "frozen", "cluster_id": cluster_id,
                    "reason": f"опубликован {age:.0f} ч назад, окно {window:.0f} ч"}

    # считаем по ВЛАДЕЛЬЦАМ: три газеты одного холдинга остаются одной ссылкой
    known_owners = {o for o in (post["posted_source_ids"] or [])}
    current_owners = {owner_of(a) for a in articles}
    added = sorted(current_owners - known_owners)

    was_one_sided = bool(post.get("one_sided"))
    now_one_sided = is_one_sided(articles)
    one_sided_changed = was_one_sided and not now_one_sided

    if not added and not one_sided_changed:
        return {"status": "unchanged", "cluster_id": cluster_id}

    from .entities import render_cards_html

    with connect() as conn:
        saved = conn.execute(
            "SELECT * FROM entities WHERE id = ANY(%s)",
            (list(post.get("entity_ids") or []),),
        ).fetchall()
    text = compose_html(
        post["header_md"], articles, post["category"],
        # пометку снимаем, если появился источник с другой стороны
        one_sided=was_one_sided and not one_sided_changed,
        significance=post.get("significance") or "",
        cards_html=render_cards_html([dict(r) for r in saved],
                                     post.get("entity_context")),
        cards=[dict(r) for r in saved],
        related_md=post.get("related_md") or "",
        geo_tag=post.get("geo_tag"),
    )
    if dry_run:
        log.info("DRY-RUN: сюжет %s дополнился (%s)%s — правка не отправлена",
                 cluster_id, ", ".join(added) or "—",
                 ", снята пометка об одном лагере" if one_sided_changed else "")
        return {"status": "dry-run", "added": added, "html": text,
                "one_sided_lifted": one_sided_changed}

    channel = env("TELEGRAM_CHANNEL_ID", required=True)
    try:
        edit_message_text(channel, int(post["message_id"]), text)
    except Exception as exc:  # noqa: BLE001 — одна неудачная правка не роняет прогон
        # «not modified» — это не сбой: источник добавился, а видимый текст
        # от этого не изменился. Считать такое ошибкой нельзя: список
        # источников тогда не запишется, и следующий прогон зайдёт на тот же
        # круг — пост будет биться о Telegram каждые полчаса бесконечно.
        if "not modified" not in str(exc):
            log.warning("Не удалось поправить пост сюжета %s: %s", cluster_id, exc)
            return {"status": "error", "cluster_id": cluster_id, "error": str(exc)}
        log.info("Сюжет %s: источники добавились, текст поста не изменился", cluster_id)

    with connect() as conn:
        conn.execute(
            """
            UPDATE posts SET posted_source_ids = %s, edited_at = now(),
                   edit_count = edit_count + 1, one_sided = %s
            WHERE id = %s
            """,
            (json.dumps(sorted(current_owners)),
             was_one_sided and not one_sided_changed, post["id"]),
        )
        record_sources(conn, post["id"], articles)

    log.info("Сюжет %s дополнен: %s%s", cluster_id, ", ".join(added) or "—",
             ", снята пометка об одном лагере" if one_sided_changed else "")
    return {"status": "edited", "cluster_id": cluster_id, "added": added,
            "one_sided_lifted": one_sided_changed}


def backfill_entity_context(entity_id: str, dry_run: bool = True) -> dict[str, Any]:
    """Добавляет пояснение в уже вышедшие посты с этим именем.

    Пул фактов появляется позже, чем выходит пост: имя попадает в очередь,
    владелец разбирает её вечером, а пост с этим именем уже висит в канале
    без пояснения. Здесь эти посты дособираются — вместе со звёздочкой у
    имени, потому что она ставится тем же compose_html.

    Контекст собирается для каждого поста заново, под его тему: одна и та же
    фигура в новости про газету и в новости про стадион требует разных фактов
    из одного пула. Это вызов модели на пост, поэтому и существует
    `entities.backfill_max_posts`.

    Правка сообщения уведомление не шлёт, так что читателя это не беспокоит.
    """
    from .config import env, get_settings
    from .entities import mark_entities, render_cards_html
    from .facts import build_context
    from .llm import LLMUsage

    s = get_settings()
    window = float(s.get_path("entities.backfill_window_hours", 168))
    max_posts = int(s.get_path("entities.backfill_max_posts", 20))
    max_cards = int(s.get_path("entities.max_cards_per_post", 2))

    stats: dict[str, Any] = {"entity_id": entity_id, "checked": 0, "edited": 0,
                             "skipped_full": 0, "skipped_empty": 0, "errors": 0,
                             "posts": [], "cost_usd": 0.0}

    with connect() as conn:
        ent = conn.execute(
            "SELECT * FROM entities WHERE id = %s", (entity_id,)
        ).fetchone()
        if ent is None:
            return {**stats, "status": "skip", "reason": "сущности нет"}
        if ent["never_explain"]:
            # имена, которые аудитория заведомо знает, объяснять не надо
            return {**stats, "status": "skip", "reason": "помечено never_explain"}
        from .facts import has_pool
        if not has_pool(conn, entity_id):
            return {**stats, "status": "skip", "reason": "пул фактов пуст"}

        # Ищем по тексту поста, а не по entity_mentions: сущности ещё не было,
        # когда пост выходил, поэтому упоминание в ту таблицу не попало —
        # ровно в этом случае пояснение и нужно задним числом.
        published = conn.execute(
            """
            SELECT * FROM posts
            WHERE status = 'published' AND message_id IS NOT NULL
              AND published_at >= now() - make_interval(hours => %s)
            ORDER BY published_at DESC
            """,
            (int(window),),
        ).fetchall()

    # Отбираем тем же mark_entities, который потом поставит звёздочку: если
    # он текст не меняет, звёздочке взяться неоткуда, и править нечего.
    ent_d = dict(ent)
    rows = [
        p for p in published
        if entity_id not in (p.get("entity_ids") or [])
        and mark_entities(p["header_md"] or "", [ent_d]) != (p["header_md"] or "")
    ]

    stats["checked"] = len(rows)
    if len(rows) > max_posts:
        # молчаливое усечение читается как «обошли всё», поэтому говорим вслух
        log.info("Сущность %s: постов %s, правим первые %s",
                 entity_id, len(rows), max_posts)
        rows = rows[:max_posts]

    channel = env("TELEGRAM_CHANNEL_ID", required=True) if not dry_run else ""
    usage = LLMUsage()

    for post in rows:
        current = list(post.get("entity_ids") or [])
        if len(current) >= max_cards:
            # больше двух пояснений превращают пост в справочник
            stats["skipped_full"] += 1
            continue

        with connect() as conn:
            articles = cluster_articles(conn, post["cluster_id"])
            saved = conn.execute(
                "SELECT * FROM entities WHERE id = ANY(%s)",
                (current + [entity_id],),
            ).fetchall()
            built = build_context(conn, ent_d, post["category"] or "",
                                  (post["header_md"] or "").split("\n")[0], usage)
        if not built:
            # под тему этого поста подходящего факта не нашлось — пропускаем
            stats["skipped_empty"] += 1
            continue

        cards = [dict(r) for r in saved]
        contexts = {**(post.get("entity_context") or {}), entity_id: built}

        text = compose_html(
            post["header_md"], articles, post["category"],
            one_sided=bool(post.get("one_sided")),
            significance=post.get("significance") or "",
            cards_html=render_cards_html(cards, contexts), cards=cards,
            related_md=post.get("related_md") or "",
            geo_tag=post.get("geo_tag"),
        )

        if dry_run:
            stats["posts"].append({"post_id": post["id"], "html": text,
                                   "context": built["context"]})
            stats["edited"] += 1
            continue

        try:
            edit_message_text(channel, int(post["message_id"]), text)
        except Exception as exc:  # noqa: BLE001 — один пост не роняет остальные
            log.warning("Сущность %s: пост %s не поправился: %s",
                        entity_id, post["id"], exc)
            stats["errors"] += 1
            continue

        with connect() as conn:
            conn.execute(
                "UPDATE posts SET entity_ids = %s, entity_context = %s, "
                "edited_at = now(), edit_count = edit_count + 1 WHERE id = %s",
                (json.dumps(current + [entity_id]),
                 json.dumps(contexts, ensure_ascii=False), post["id"]),
            )
            conn.execute(
                "UPDATE entity_mentions SET shown = TRUE "
                "WHERE post_id = %s AND entity_id = %s", (post["id"], entity_id),
            )
            conn.execute(
                "UPDATE entities SET last_explained_at = now() WHERE id = %s",
                (entity_id,),
            )
        stats["edited"] += 1
        stats["posts"].append({"post_id": post["id"], "message_id": post["message_id"]})

    stats["cost_usd"] = round(usage.cost_usd, 4)

    if stats["edited"] or stats["checked"]:
        log.info("Сущность %s: пояснение добавлено в %s постов из %s "
                 "(полных: %s, без подходящих фактов: %s, ошибок: %s)",
                 entity_id, stats["edited"], stats["checked"],
                 stats["skipped_full"], stats["skipped_empty"], stats["errors"])
    return {**stats, "status": "ok"}


def refresh_posts_with_entity(entity_id: str, *, window_hours: float | None = None,
                              reassemble: bool = False) -> int:
    """Пересобирает посты, которые показывают эту сущность.

    Нужна после правки факта, после его отставки и после удаления сущности:
    пояснение уже висит в вышедших постах, и без пересборки читатель видит
    старый текст или пояснение к тому, чего больше нет.

    `reassemble` зовёт сборщик заново — это нужно, когда изменился сам пул
    (процессуальный статус, правка факта). По умолчанию текст берётся тот,
    что уже сохранён на посте: пересобирать одно и то же значит платить
    за модель и получать каждый раз другую формулировку под тем же
    сообщением.

    Окно правки существует потому, что дальше пост никто не открывает,
    а каждая правка — это вызов API.
    """
    from .config import env, get_settings
    from .entities import render_cards_html
    from .facts import build_context
    from .llm import LLMUsage

    if window_hours is None:
        window_hours = float(get_settings().get_path(
            "entities.backfill_window_hours", 168))

    with connect() as conn:
        posts_ = conn.execute(
            """
            SELECT * FROM posts
            WHERE status = 'published' AND message_id IS NOT NULL
              AND entity_ids ? %s
              AND published_at >= now() - make_interval(hours => %s)
            """,
            (entity_id, int(window_hours)),
        ).fetchall()
        alive = {r["id"] for r in conn.execute(
            "SELECT id FROM entities WHERE id = ANY(%s)",
            ([entity_id],),
        ).fetchall()}

    if not posts_:
        return 0

    channel = env("TELEGRAM_CHANNEL_ID", required=True)
    usage = LLMUsage()
    edited = 0
    for post in posts_:
        # удалённую сущность выкидываем из списка: пояснять больше нечего
        ids = [e for e in (post.get("entity_ids") or [])
               if e != entity_id or entity_id in alive]
        contexts = {k: v for k, v in (post.get("entity_context") or {}).items()
                    if k in ids}

        with connect() as conn:
            articles = cluster_articles(conn, post["cluster_id"])
            cards = [dict(r) for r in conn.execute(
                "SELECT * FROM entities WHERE id = ANY(%s)", (ids,),
            ).fetchall()]
            if reassemble and entity_id in ids:
                ent = next((c for c in cards if c["id"] == entity_id), None)
                built = build_context(conn, ent, post["category"] or "",
                                      (post["header_md"] or "").split("\n")[0],
                                      usage) if ent else None
                # факта не осталось — снимаем пояснение, а не показываем старое
                contexts.pop(entity_id, None)
                if built:
                    contexts[entity_id] = built

        text = compose_html(
            post["header_md"], articles, post["category"],
            one_sided=bool(post.get("one_sided")),
            significance=post.get("significance") or "",
            cards_html=render_cards_html(cards, contexts), cards=cards,
            related_md=post.get("related_md") or "", geo_tag=post.get("geo_tag"),
        )
        try:
            edit_message_text(channel, int(post["message_id"]), text)
        except Exception as exc:  # noqa: BLE001
            if "not modified" not in str(exc):
                log.warning("Пост %s не пересобрался: %s", post["id"], exc)
            continue
        with connect() as conn:
            conn.execute(
                "UPDATE posts SET entity_ids = %s, entity_context = %s, "
                "edited_at = now(), edit_count = edit_count + 1 WHERE id = %s",
                (json.dumps(ids), json.dumps(contexts, ensure_ascii=False),
                 post["id"]),
            )
        edited += 1

    log.info("Сущность %s: пересобрано постов %s", entity_id, edited)
    return edited


def refresh_published(dry_run: bool = True, window_hours: float | None = None
                      ) -> dict[str, Any]:
    """Пересобирает текст вышедших постов под текущие правила рендера.

    Нужна, когда меняется не содержание, а способ показа: появился гео-тег,
    ужесточилось правило significance. Правка сообщения уведомление не шлёт,
    поэтому читателя это не беспокоит.

    Отправляем только если текст реально изменился: пустая правка — лишний
    вызов API и лишняя строчка «изменено» под постом.
    """
    from .config import env, get_settings
    from .entities import render_cards_html

    if window_hours is None:
        window_hours = float(get_settings().get_path("entities.backfill_window_hours", 168))

    stats: dict[str, Any] = {"checked": 0, "edited": 0, "unchanged": 0, "errors": 0,
                             "cleaned": 0, "items": []}

    with connect() as conn:
        posts_ = conn.execute(
            """
            SELECT * FROM posts
            WHERE status = 'published' AND message_id IS NOT NULL
              AND published_at >= now() - make_interval(hours => %s)
            ORDER BY id
            """,
            (int(window_hours),),
        ).fetchall()

    stats["checked"] = len(posts_)
    channel = env("TELEGRAM_CHANNEL_ID", required=True) if not dry_run else ""

    for post in posts_:
        significance = clean_significance(post.get("significance") or "")
        dropped = bool((post.get("significance") or "").strip()) and not significance

        with connect() as conn:
            articles = cluster_articles(conn, post["cluster_id"])
            cards = [dict(r) for r in conn.execute(
                "SELECT * FROM entities WHERE id = ANY(%s)",
                (list(post.get("entity_ids") or []),),
            ).fetchall()]

        text = compose_html(
            post["header_md"], articles, post["category"],
            one_sided=bool(post.get("one_sided")), significance=significance,
            cards_html=render_cards_html(cards, post.get("entity_context")),
            cards=cards,
            related_md=post.get("related_md") or "",
            geo_tag=post.get("geo_tag"),
        )
        if dropped:
            stats["cleaned"] += 1
        stats["items"].append({"post_id": post["id"], "message_id": post["message_id"],
                               "dropped_significance": dropped,
                               "geo_tag": post.get("geo_tag")})
        if dry_run:
            stats["edited"] += 1
            continue

        try:
            edit_message_text(channel, int(post["message_id"]), text)
        except Exception as exc:  # noqa: BLE001 — один пост не роняет остальные
            # Сравнивать текст самим значило бы хранить отправленный HTML и
            # держать его в согласии со всеми местами правки. Телеграм и так
            # отвергает правку, не меняющую сообщение, — этого достаточно.
            if "not modified" in str(exc):
                stats["unchanged"] += 1
                continue
            log.warning("Пост %s не пересобрался: %s", post["id"], exc)
            stats["errors"] += 1
            continue

        with connect() as conn:
            conn.execute(
                "UPDATE posts SET significance = %s, edited_at = now(), "
                "edit_count = edit_count + 1 WHERE id = %s",
                (significance, post["id"]),
            )
        stats["edited"] += 1

    log.info("Пересборка: проверено %s, изменено %s, вычищено significance %s",
             stats["checked"], stats["edited"], stats["cleaned"])
    return stats


def sync_all(dry_run: bool = True) -> dict[str, Any]:
    """Проходит по опубликованным постам и дополняет те, где появились издания."""
    with connect() as conn:
        pending = pending_updates(conn)

    stats = {"checked": len(pending), "edited": 0, "errors": 0, "added": []}
    for row in pending:
        res = sync_post(row["cluster_id"], dry_run=dry_run)
        if res["status"] == "edited":
            stats["edited"] += 1
            stats["added"].append({row["cluster_id"]: res["added"]})
        elif res["status"] == "error":
            stats["errors"] += 1
    log.info("Дополнено постов: %s из %s кандидатов", stats["edited"], stats["checked"])
    return stats


# ------------------------------------------------------------------ автопостинг

LEFT_STRICT = ("far-left", "left")
RIGHT_STRICT = ("right", "far-right")
LEFT_BROAD = ("far-left", "left", "center-left")
RIGHT_BROAD = ("center-right", "right", "far-right")


def _in_publish_window(now) -> bool:
    """Прогон публикует только внутри окна по Мадриду."""
    from .config import get_settings

    s = get_settings()
    lo = int(s.get_path("autopost.window_from_hour", 0))
    hi = int(s.get_path("autopost.window_to_hour", 24))
    return lo <= now.hour < hi


def _quota_state(conn, now) -> tuple[int, str]:
    """Сколько постов ещё можно сейчас и почему.

    Окно скользящее: считаем за последние 24 часа, а не с полуночи. Иначе
    полночь становится дырой, куда можно высыпать двойную норму.
    """
    from .config import get_settings

    s = get_settings()
    cap_day = int(s.get_path("autopost.max_posts_per_day", 25))
    cap_early = int(s.get_path("autopost.max_posts_before_evening", cap_day))
    evening_from = int(s.get_path("autopost.evening_from_hour", 24))

    used_24h = conn.execute(
        "SELECT count(*) AS n FROM posts WHERE status = 'published' "
        "AND published_at >= now() - interval '24 hours'"
    ).fetchone()["n"]

    room = cap_day - int(used_24h)
    reason = f"за 24 ч опубликовано {used_24h} из {cap_day}"

    if now.hour < evening_from:
        # резерв на вечер считается по календарному дню: он про «сегодня
        # до вечера», а не про скользящее окно
        used_today = conn.execute(
            "SELECT count(*) AS n FROM posts WHERE status = 'published' "
            "AND published_at >= date_trunc('day', now())"
        ).fetchone()["n"]
        early_room = cap_early - int(used_today)
        if early_room < room:
            room = early_room
            reason = (f"до {evening_from}:00 лимит {cap_early}, "
                      f"уже {used_today} — держим резерв на вечер")

    return max(0, room), reason


def _minutes_since_last_post(conn) -> float | None:
    row = conn.execute(
        "SELECT EXTRACT(EPOCH FROM (now() - max(published_at)))/60 AS m "
        "FROM posts WHERE status = 'published'"
    ).fetchone()
    return float(row["m"]) if row and row["m"] is not None else None


def _cluster_pool(conn, *, ignore_time: bool = False,
                  include_expired: bool = False) -> list[dict[str, Any]]:
    """Открытые сюжеты без поста, вместе с их источниками.

    Ветки допуска и рейтинг считаются в Python через spectrum: SQL не должен
    знать про шкалу, бакеты и владельцев — это конфиг, а не схема.
    """
    from datetime import datetime, timezone

    from .config import get_settings
    from .spectrum import is_one_sided, n_owners, passes_rule, score, span

    min_age = int(get_settings().get_path("autopost.min_age_minutes", 0))
    age_clause = "" if ignore_time else (
        "AND min(a.published_at) <= now() - make_interval(mins => %(min_age)s)"
    )

    rows = conn.execute(
        f"""
        SELECT c.id AS cluster_id,
               count(DISTINCT a.source_id) AS n_sources,
               count(*)                    AS n_articles,
               min(a.published_at)         AS first_at,
               max(a.published_at)         AS last_at,
               p.id                        AS last_post_id,
               p.status                    AS last_post_status,
               p.published_at              AS last_published_at,
               p.n_articles_at_publish     AS n_at_publish,
               p.message_id                AS last_message_id,
               json_agg(DISTINCT jsonb_build_object(
                   'source_id', s.id, 'lean', s.lean, 'type', s.type,
                   'owner_group', s.owner_group)) AS sources,
               array_agg(DISTINCT a.title)       AS titles
        FROM clusters c
        JOIN articles a ON a.cluster_id = c.id
        JOIN sources  s ON s.id = a.source_id
        LEFT JOIN LATERAL (
            SELECT * FROM posts p2 WHERE p2.cluster_id = c.id
            ORDER BY p2.published_at DESC NULLS FIRST LIMIT 1
        ) p ON TRUE
        WHERE c.status = 'open'
          -- не добрал источников за окно повышения — постом уже не станет,
          -- но строкой в «Коротко» ещё может выйти
          AND (c.expired_at IS NULL OR %(include_expired)s)
          -- сюжет, ушедший в дайджест, отдельным постом уже не выйдет (§5)
          AND (p.id IS NULL OR p.status = 'published')
        GROUP BY c.id, p.id, p.status, p.published_at, p.n_articles_at_publish,
                 p.message_id
        HAVING TRUE {age_clause}
        """,
        {"min_age": min_age, "include_expired": include_expired},
    ).fetchall()

    now = datetime.now(timezone.utc)
    for r in rows:
        srcs = r["sources"]
        r["junk"] = is_junk(r.get("titles") or [])
        r["repeat_ok"], r["is_continuation"] = repeat_state(r)
        age_h = (now - r["first_at"]).total_seconds() / 3600 if r["first_at"] else 0.0
        r["n_owners"] = n_owners(srcs)
        r["span"] = span(srcs)
        r["one_sided"] = is_one_sided(srcs)
        r["score"] = score(srcs, age_h)
        r["passes"] = passes_rule(srcs)

    # ежедневные рубрики выбрасываем сразу: они не должны ни попадать в посты,
    # ни занимать строки в дайджесте
    rows = [r for r in rows if not r["junk"] and r["repeat_ok"]]
    # по убыванию скора: он уже учитывает и владельцев, и размах, и свежесть
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


@lru_cache(maxsize=1)
def _junk_patterns() -> tuple:
    import re as _re

    from .config import get_settings

    pats = get_settings().get_path("autopost.exclude_title_patterns", []) or []
    return tuple(_re.compile(p) for p in pats)


def is_junk(titles: list[str]) -> bool:
    """Ежедневные рубрики: лотереи, гороскопы, розыгрыши.

    Их печатают все издания со всех флангов, поэтому любая ветка допуска
    проходится тривиально. Отсекаем по заголовку: это не новость, а сервисная
    страница, и её место не в канале про повестку.
    """
    pats = _junk_patterns()
    return any(p.search(t or "") for p in pats for t in titles)


def in_morning_review(now) -> bool:
    """Прогон 09:00 — тот, где пересматриваются вчерашние вечерние сюжеты."""
    from .config import get_settings

    s = get_settings()
    hour = int(s.get_path("autopost.morning_review_hour", 9))
    return now.hour == hour


def expire_stale_candidates(conn, now) -> int:
    """Помечает expired то, что за отведённое время НЕ набрало источников.

    Окно касается только сюжетов, не прошедших ветки допуска. Прошедший, но
    не попавший в квоту, не истекает: он остаётся кандидатом и проигрывает
    свежему по скору — этого достаточно, отдельно вычёркивать его нельзя,
    иначе крупная новость пропадёт из-за занятой квоты.

    Исключение — утренний пересмотр: сюжеты, начавшиеся вчера после
    morning_review_from_hour, доживают до утреннего прогона независимо от
    окна, потому что ответ оппонирующих изданий приходит в утренних выпусках.
    """
    from datetime import timedelta

    from .config import get_settings
    from .spectrum import passes_rule

    s = get_settings()
    window = int(s.get_path("autopost.promotion_window_hours", 4))
    review_from = int(s.get_path("autopost.morning_review_from_hour", 17))
    morning = in_morning_review(now)

    rows = conn.execute(
        """
        SELECT c.id, c.first_seen_at,
               json_agg(DISTINCT jsonb_build_object(
                   'source_id', s.id, 'lean', s.lean, 'type', s.type,
                   'owner_group', s.owner_group)) AS sources
        FROM clusters c
        JOIN articles a ON a.cluster_id = c.id
        JOIN sources  s ON s.id = a.source_id
        WHERE c.expired_at IS NULL
          AND c.first_seen_at < now() - make_interval(hours => %s)
          AND NOT EXISTS (SELECT 1 FROM posts p WHERE p.cluster_id = c.id)
        GROUP BY c.id
        """,
        (window,),
    ).fetchall()

    yesterday_evening = (now - timedelta(days=1)).replace(
        hour=review_from, minute=0, second=0, microsecond=0
    )
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    doomed = []
    for r in rows:
        if passes_rule(r["sources"]):
            continue  # прошёл ветки — ждёт квоты, не истекает
        # до утреннего пересмотра вчерашним вечерним даём дожить
        if not morning and yesterday_evening <= r["first_seen_at"] < today_start:
            continue
        doomed.append(r["id"])

    if doomed:
        conn.execute(
            "UPDATE clusters SET expired_at = now() WHERE id = ANY(%s)", (doomed,)
        )
    return len(doomed)


def repeat_state(row: dict[str, Any]) -> tuple[bool, bool]:
    """(допустить ли, продолжение ли) по правилу повтора §2.

    Условия соединены И, а не ИЛИ: одного времени мало — если о сюжете
    перестали писать, повторять его незачем; одних новых статей тоже мало —
    иначе живой сюжет вылезал бы в канал каждый час.
    """
    from datetime import datetime, timezone

    from .config import get_settings

    if row.get("last_published_at") is None:
        return True, False  # ещё не публиковался

    s = get_settings()
    min_new = int(s.get_path("autopost.repeat_min_new_articles", 3))
    min_hours = float(s.get_path("autopost.repeat_min_hours", 12))

    new_articles = int(row["n_articles"]) - int(row.get("n_at_publish") or 0)
    hours = (datetime.now(timezone.utc) - row["last_published_at"]).total_seconds() / 3600

    return (new_articles >= min_new and hours >= min_hours), True


def matches_rule(row: dict[str, Any]) -> bool:
    """Прошёл ли сюжет ветки допуска. Считается в _cluster_pool через spectrum."""
    return bool(row.get("passes"))


def eligible_clusters(conn, *, ignore_time: bool = False) -> list[dict[str, Any]]:
    """Сюжеты на отдельный пост."""
    return [r for r in _cluster_pool(conn, ignore_time=ignore_time) if r["passes"]]


def digest_clusters(conn, *, ignore_time: bool = False) -> list[dict[str, Any]]:
    """Строки для блока «Коротко». Два потока (§5).

    Поток 1 — не дозревшие до поста: несколько владельцев, но веток допуска
    не прошли, и с первой статьи прошло достаточно, чтобы шанс дозреть уже
    был использован.

    Поток 2 — заграница без испанского угла: постом такие не станут в любом
    случае, ждать нечего, поэтому выдержка к ним не применяется.
    """
    from datetime import datetime, timezone

    from .config import get_settings
    from .spectrum import n_owners, political_sources

    s = get_settings()
    min_owners = int(s.get_path("digest.min_sources", 2))
    max_lines = int(s.get_path("digest.max_lines", 12))
    max_world = int(s.get_path("digest.max_world", 4))
    min_age = float(s.get_path("digest.min_age_hours", 6))
    max_age = float(s.get_path("digest.max_age_hours", 30))
    now = datetime.now(timezone.utc)

    # уже попавшее в «Коротко» второй раз не берём: дедуп по сюжету навсегда
    seen = {r["cluster_id"] for r in conn.execute(
        "SELECT DISTINCT cluster_id FROM digest_items WHERE cluster_id IS NOT NULL"
    ).fetchall()}

    flow1, flow2 = [], []
    for r in _cluster_pool(conn, ignore_time=True, include_expired=True):
        if r["cluster_id"] in seen:
            continue
        age = (now - r["first_at"]).total_seconds() / 3600 if r["first_at"] else 0.0
        # слишком старое выбывает: вчерашняя мелочь сегодня уже не новость
        if age > max_age:
            continue

        if r.get("scope") == "world" and r["passes"]:
            flow2.append(r)
            continue
        if r["passes"]:
            continue  # это кандидат в отдельный пост, а не в «Коротко»
        if n_owners(political_sources(r["sources"])) < min_owners:
            continue
        # сюжету моложе min_age ещё может достаться собственный пост
        if age < min_age:
            continue
        flow1.append(r)

    flow2.sort(key=lambda r: r["score"], reverse=True)
    flow1.sort(key=lambda r: r["score"], reverse=True)
    # мировым отводим долю: иначе в живой мировой день «Коротко»
    # перестаёт быть про Испанию
    picked = flow2[:max_world] + flow1
    picked.sort(key=lambda r: r["score"], reverse=True)
    return picked[:max_lines]


def autopost_enabled() -> bool:
    """Включена ли автопубликация.

    Переменная окружения важнее конфига. Причина операционная: конфиг лежит
    в репозитории, и любое обновление сервера через `git reset --hard`
    затирает правку — публикация выключается молча, и заметить это можно
    только по тишине в канале. В .env, который в git не попадает, такого
    не случится.
    """
    raw = env("AUTOPOST_ENABLED", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return bool(get_settings().get_path("autopost.enabled", False))


def send_post_for_review(cluster_id: int, cards: list[dict[str, Any]] | None = None) -> None:
    """Готовый пост уходит владельцу с кнопками вместо канала (§9)."""
    from .entities import render_cards_html
    from .telegram import notify_owner

    with connect() as conn:
        post = get_post(conn, cluster_id)
        articles = cluster_articles(conn, cluster_id)
    if post is None:
        return

    body = compose_html(
        post["header_md"], articles, post["category"],
        one_sided=bool(post.get("one_sided")),
        significance=post.get("significance") or "",
        cards_html=render_cards_html(cards or [], post.get("entity_context")),
        cards=cards or [],
        geo_tag=post.get("geo_tag"),
    )
    notify_owner(
        f"<b>Черновик поста</b>\n\n{body}",
        reply_markup={"inline_keyboard": [[
            {"text": "✅ Опубликовать", "callback_data": f"post:pub:{cluster_id}"},
            {"text": "⏭ Пропустить", "callback_data": f"post:skip:{cluster_id}"},
        ]]},
    )


def autopost(dry_run: bool = True) -> dict[str, Any]:  # noqa: C901
    """Отбирает сюжеты по правилу, пишет заголовок моделью и публикует.

    В dry-run печатает, что было бы опубликовано, и ничего не трогает —
    это режим по умолчанию и способ настроить правило, ничего не сломав.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from .config import get_settings
    from .spectrum import buckets

    s = get_settings()
    tz = ZoneInfo(s.require("render.timezone"))
    now = datetime.now(tz)

    stats: dict[str, Any] = {
        "enabled": autopost_enabled(),
        "candidates": 0, "published": 0, "skipped": 0, "errors": 0, "items": [],
    }

    if not _in_publish_window(now):
        stats["skipped_reason"] = f"вне окна публикации ({now:%H:%M} по Мадриду)"
        log.info("Автопостинг: %s", stats["skipped_reason"])
        return stats

    with connect() as conn:
        # просроченных убираем до отбора, чтобы они не занимали место
        stats["expired"] = expire_stale_candidates(conn, now)
        candidates = eligible_clusters(conn)
        room, reason = _quota_state(conn, now)
        gap_since = _minutes_since_last_post(conn)

    stats["candidates"] = len(candidates)
    stats["quota"] = reason
    if room <= 0:
        stats["skipped_reason"] = f"квота выбрана: {reason}"
        log.info("Автопостинг: %s", stats["skipped_reason"])
        return stats

    # пауза между постами: держим её проверкой времени, а не sleep — прогон
    # в раннере не должен простаивать двадцать минут, это оплаченные минуты
    min_gap = float(s.get_path("autopost.min_gap_minutes", 0))
    if not dry_run and gap_since is not None and gap_since < min_gap:
        stats["skipped_reason"] = (
            f"с прошлого поста {gap_since:.0f} мин, нужен разрыв {min_gap:.0f}"
        )
        log.info("Автопостинг: %s", stats["skipped_reason"])
        return stats

    room = min(room, int(s.get_path("autopost.max_posts_per_run", 1)))

    review_mode = str(env("REVIEW_MODE", "")).lower() in ("1", "true", "yes")
    sound_cap = int(s.get_path("autopost.sound.max_per_day", 2))
    granted = 0  # сколько звуков выдано в этом прогоне

    for row in candidates[:room]:
        cid = row["cluster_id"]
        try:
            header, topic, meta = generate_header(cid)
        except Exception as exc:  # noqa: BLE001 — один сюжет не роняет остальные
            log.warning("Сюжет %s: заголовок не сгенерировался: %s", cid, exc)
            stats["errors"] += 1
            continue

        # scope пригодится вечернему посту — сохраняем на кластере
        if meta.get("scope"):
            with connect() as conn:
                conn.execute("UPDATE clusters SET scope = %s WHERE id = %s",
                             (meta["scope"], cid))

        # world не получает отдельного поста никогда — только строку в «Коротко»
        if meta.get("scope") in NO_POST_SCOPES:
            log.info("Сюжет %s: scope=%s — в отдельный пост не идёт",
                     cid, meta["scope"])
            stats["skipped_world"] = stats.get("skipped_world", 0) + 1
            continue

        # ворота качества: непройденный пост не выходит, причина — в чат ревью,
        # остальные посты прогона это не блокирует (§8)
        with connect() as conn:
            arts = cluster_articles(conn, cid)
        report = check_post(
            cluster_id=cid,
            headline=meta.get("headline", ""),
            summary=meta.get("lead", ""),
            significance=meta.get("significance", ""),
            body_md=compose_md(header, arts, topic,
                               one_sided=row["one_sided"],
                               significance=meta.get("significance", "")),
            articles=arts,
            sources=row["sources"],
            one_sided=row["one_sided"],
            scope=meta.get("scope"),
            geo_tag=meta.get("geo_tag"),
            entities=meta.get("entities"),
            check_urls=not dry_run,
        )
        if not report.passed:
            reason = report.reason()
            log.warning("Сюжет %s не прошёл ворота: %s", cid, reason)
            stats["gated"] = stats.get("gated", 0) + 1
            if not dry_run:
                notify_owner(
                    f"⚠️ Пост по сюжету {cid} не опубликован.\n"
                    f"<b>{meta.get('headline', '')}</b>\n\n{reason}"
                )
            continue

        with connect() as conn:
            would_sound = deserves_sound(conn, row) and granted < sound_cap
        if would_sound:
            granted += 1
        stats["items"].append({
            "cluster_id": cid, "n_sources": row["n_sources"], "sound": would_sound,
            "scope": meta.get("scope"), "geo_tag": meta.get("geo_tag"),
            "one_sided": row["one_sided"],
            "buckets": sorted(buckets(row["sources"])), "topic": topic,
            "headline": header.split("\n")[0].strip("* "),
        })

        if dry_run:
            continue

        if not stats["enabled"]:
            # правило посчитано, но публикация выключена — только черновики
            with connect() as conn:
                upsert_draft(conn, cid, header, topic,
                             significance=meta.get("significance", ""),
                             one_sided=row["one_sided"],
                             scope=meta.get("scope"), geo_tag=meta.get("geo_tag"),
                             is_continuation=row["is_continuation"])
            stats["skipped"] += 1
            continue

        with connect() as conn:
            upsert_draft(conn, cid, header, topic,
                         significance=meta.get("significance", ""),
                         one_sided=row["one_sided"],
                         scope=meta.get("scope"), geo_tag=meta.get("geo_tag"),
                         is_continuation=row["is_continuation"])
            # сущности: матчим, собираем контекст под тему этого поста и
            # запоминаем, что показали (§7.5). Собранный контекст живёт
            # на посте: пост дополняется ссылками десятки раз, и звать
            # сборщик на каждую правку значило бы платить за него десятки
            # раз и получать каждый раз другой текст.
            resolved = resolve_all(conn, meta.get("entities"), cid)
            candidates = pick_for_display(conn, resolved)
            contexts = contexts_for(conn, candidates, topic,
                                    meta.get("headline", ""), LLMUsage())
            shown = [e for e in candidates if e["id"] in contexts]
            conn.execute(
                "UPDATE posts SET entity_ids = %s, entity_context = %s "
                "WHERE cluster_id = %s AND status = 'draft'",
                (json.dumps([e["id"] for e in shown]),
                 json.dumps(contexts, ensure_ascii=False), cid))
            loud = would_sound

            # «Ранее по теме» — только если пост не уходит реплаем
            related = None
            if not row["is_continuation"] and get_settings().get_path(
                    "related.enabled", True):
                related = find_related(conn, cid, topic,
                                       [e["id"] for e in resolved])
        if stats["published"] and min_gap > 0:
            # второй пост в прогоне ждёт положенный разрыв
            import time as _time
            log.info("Пауза %s мин перед следующим постом", min_gap)
            _time.sleep(min_gap * 60)

        if review_mode:
            # первые недели пост не уходит в канал сам: показываем в чат ревью
            # и ждём кнопки. Без нажатия он просто не выйдет — это штатный
            # исход, а не поломка (§0).
            send_post_for_review(cid, shown)
            stats["sent_for_review"] = stats.get("sent_for_review", 0) + 1
            continue

        try:
            publish(cid, dry_run=False, silent=not loud, cards=shown,
                    related=related)
            with connect() as conn:
                mark_shown(conn, resolved, cid, None, {e["id"] for e in shown})
            stats["published"] += 1
            if shown:
                stats["cards"] = stats.get("cards", 0) + len(shown)
            if loud:
                stats["with_sound"] = stats.get("with_sound", 0) + 1
        except Exception as exc:  # noqa: BLE001
            log.warning("Сюжет %s не опубликовался: %s", cid, exc)
            stats["errors"] += 1

    # очередь неразрешённых — рабочий список владельца, а не служебная таблица:
    # о пополнении надо сказать, иначе о ней никто не вспомнит
    if not dry_run:
        with connect() as conn:
            stats["unresolved_notified"] = notify_new_unresolved(conn)

    log.info(
        "Автопостинг: кандидатов %s, опубликовано %s, черновиков %s, ошибок %s",
        stats["candidates"], stats["published"], stats["skipped"], stats["errors"],
    )
    return stats
