"""Пул проверенных фактов о сущностях и сборка контекста под пост.

Пайплайн разваливается на две части с разной экономикой.

**Проверка факта** — дорогая, разовая. Факт получает дословную цитату из
источника, проходит детерминированный слой А и критика со скриптовым
вердиктом (слой Б) и живёт годами.

**Сборка контекста** — дешёвая, на каждый пост. Из пула выбираются два факта,
ближайших к теме сюжета, и склеиваются в строку. Сборщик не видит источников
вовсе: всё, что он может написать, уже проверено, потому что взято из пула.
Поэтому повторная верификация не нужна, а появление в собранной строке слова,
которого нет в исходных фактах, — ошибка, а не вольность.

Два инварианта, которые нарушать нельзя:

1. Ни одного утверждения без источника. Собственные знания модели источником
   не являются никогда.
2. Сборщик не изобретает. Он выбирает и соединяет уже проверенное.
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .config import CONFIG_DIR, get_settings, load_prompt
from .textutil import longest_common_shingle, normalize_words
from .wordlists import hits, load, normalize, words

log = logging.getLogger(__name__)

KINDS = ("role", "scale", "classification", "evaluative", "legal")

# Что разрешает каждый уровень источника (§5 спеки).
#
# `official` годится для фактического каркаса и не годится для характеристики:
# должность, отрасль, дата основания — берём; место в спектре, тип издания,
# репутация — никогда, потому что ни одна партия не назовёт себя ультраправой.
#
# `press` разрешает только должность как приложение при имени и оценку
# с атрибуцией. Масштаб, классификация и процессуальный статус из газетной
# статьи — это пересказ чужого пересказа.
TIER_ALLOWS: dict[str, tuple[str, ...]] = {
    "wiki": KINDS,
    "wiki_org": KINDS,
    "wikidata": ("role", "scale", "classification"),
    "official": ("role", "scale", "legal"),
    "press": ("role", "evaluative"),
    # старые карточки, перенесённые миграцией: показывать их нельзя,
    # но и терять незачем
    "legacy": (),
}

TOPICS = (
    "политика", "экономика", "медиа", "спорт", "право", "общество",
    "регионы", "международное",
)

# Рейтинговые места: читателю не с чем их сравнить, и они тухнут за год.
_RANKING = [
    (re.compile(r"\bтоп[-\s]?\d+", re.I), "место в рейтинге"),
    (re.compile(r"№\s*\d+"), "номер в списке"),
    (re.compile(r"\b\d+-?[яйеы]\s+(?:по|в)\s", re.I), "порядковое место"),
    (re.compile(r"\b(?:перв|втор|трет|четвёрт|пят)[а-яё]*\s+по\s+"
                r"(?:величине|тиражу|обороту|выручке|капитализации)", re.I),
     "место по показателю"),
    (re.compile(r"\bForbes\b|\bFortune\b", re.I), "ссылка на рейтинг издания"),
]

# Абсолютные числа: состояния, капитализация, тиражи. Требуют ежегодного
# обновления и ничего не объясняют. Качественная оценка масштаба разрешена.
_NUMBERS = [
    (re.compile(r"\d[\d\s.,]*\s*(?:млн|млрд|тыс\.?|миллион|миллиард)", re.I),
     "абсолютный числовой показатель"),
    (re.compile(r"[€$]\s*\d|\d\s*(?:евро|долларов)", re.I), "денежная сумма"),
    (re.compile(r"\bс\s+(?:19|20)\d{2}\s+год", re.I), "дата назначения"),
    (re.compile(r"\bв\s+(?:19|20)\d{2}\s+год[ау]\s+(?:возглав|стал|назнач|избран)", re.I),
     "дата назначения"),
]

# Причастность без процессуальной формы. Допустима только как kind=legal
# и только формулировкой из словаря.
_GUILT = [
    (re.compile(r"\bзамешан", re.I), "утверждение о причастности"),
    (re.compile(r"\bпричаст", re.I), "утверждение о причастности"),
    (re.compile(r"\bуличён|\bуличен|\bуличена", re.I), "утверждение о вине"),
    (re.compile(r"\bкоррупционер|\bвзяточник|\bмошенник", re.I),
     "обвинительная характеристика"),
    (re.compile(r"\bмахинац", re.I), "обвинительная характеристика"),
]


def cfg(key: str, default: Any = None) -> Any:
    return get_settings().get_path(f"facts.{key}", default)


@lru_cache(maxsize=1)
def legal_terms() -> tuple[dict[str, Any], ...]:
    """Закрытый словарь процессуальных формулировок.

    Генератор не формулирует статус своими словами: `investigado` — не
    «обвиняемый», термин ввели в 2015 взамен `imputado` именно потому, что
    старый стигматизировал человека, формально ещё ни в чём не обвинённого.
    Русское «обвиняемый» возвращает ту самую стигму, и никакая цитата
    из источника этого не оправдывает.
    """
    path = Path(CONFIG_DIR) / "legal_terms.yaml"
    if not path.exists():
        log.warning("Нет config/legal_terms.yaml — kind=legal будет отклоняться весь")
        return ()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return tuple(raw)


def match_legal_term(fact: str) -> dict[str, Any] | None:
    """Какой формулировке словаря соответствует факт. None — ни одной.

    Возвращает запись словаря вместе с распознанным названием дела: по ней
    видно и стадию, и то, исход это или ещё нет.

    Длинные шаблоны проверяются первыми. Иначе «осуждён по делу {case}»
    поглотил бы «осуждён по делу {case}, приговор не вступил в силу», и
    приговор, который ещё обжалуется, подавался бы как окончательный —
    это фактическая ошибка, а не стилистическая неточность.
    """
    norm = normalize(fact)
    terms = sorted(legal_terms(), key=lambda t: len(str(t.get("ru", ""))), reverse=True)

    for term in terms:
        template = str(term.get("ru", ""))
        if not template:
            continue
        head, sep, tail = template.partition("{case}")
        head_n, tail_n = normalize(head), normalize(tail)

        if head_n and head_n not in norm:
            continue
        if not sep:
            return {**term, "case": ""}

        rest = norm.split(head_n, 1)[1] if head_n else norm
        if tail_n:
            if tail_n not in rest:
                continue
            rest = rest.split(tail_n, 1)[0]
        if rest.strip():
            return {**term, "case": rest.strip()}
    return None


def has_outcome(source_text: str) -> bool:
    """Есть ли в источнике исход дела.

    Оправдание и прекращение — не смягчающая деталь, а текущее состояние.
    Если источник его содержит, факт обязан содержать тоже.
    """
    return bool(hits(source_text, load("legal_markers.txt")))


# --------------------------------------------------------------- слой А


def validate_fact(fact: dict[str, Any], source_text: str = "",
                  tier: str = "wiki") -> list[str]:
    """Детерминированные проверки одного факта. Без модели.

    Список проблем; пустой список — факт прошёл. Всё, что проверяется здесь,
    проверяется до всякого вызова модели: это дешевле и надёжнее, чем
    спрашивать, и не зависит от того, в каком настроении модель сегодня.
    """
    text = (fact.get("fact") or "").strip()
    kind = (fact.get("kind") or "").strip()
    attribution = (fact.get("attribution") or "").strip()
    limit = int(cfg("max_fact_chars", 120))
    fails: list[str] = []

    if not text:
        fails.append("пустой факт")
        return fails

    if kind not in KINDS:
        fails.append(f"неизвестный kind: {kind!r}")
    elif tier in TIER_ALLOWS and kind not in TIER_ALLOWS[tier]:
        fails.append(f"kind={kind} не разрешён для источника уровня {tier}")

    if len(text) > limit:
        fails.append(f"длина {len(text)} при пределе {limit}")

    for group in (_RANKING, _NUMBERS):
        for pattern, what in group:
            m = pattern.search(text)
            if m:
                fails.append(f"{what}: «{m.group(0).strip()}»")

    # Оценочные слова допустимы только там, где рядом стоит имя того, кто
    # оценку высказал.
    if kind != "evaluative":
        bad = hits(text, load("blocklist.txt"))
        if bad:
            fails.append("оценочные слова вне kind=evaluative: " + ", ".join(bad[:3]))

    # Размытая атрибуция запрещена во всех типах: она позволяет произнести
    # утверждение, не отвечая за него, и непроверяема.
    vague = hits(text, load("vague_attribution.txt"))
    if vague:
        fails.append("размытая атрибуция: " + ", ".join(vague[:3]))

    # Укоренённость в источнике не равна нейтральности: «ультраправый активист,
    # распространяющий фейки» подтверждается цитатой из El País и всё равно
    # недопустим без имени издания ВНУТРИ факта — иначе в блоке контекста
    # утверждение прозвучит от имени канала.
    if kind == "evaluative":
        if not attribution:
            fails.append("kind=evaluative без поля attribution")
        else:
            head = normalize(attribution).split()
            if not head or head[0] not in normalize(text).split():
                fails.append(f"атрибуция «{attribution}» отсутствует в тексте факта")

    if kind != "legal":
        for pattern, what in _GUILT:
            m = pattern.search(text)
            if m:
                fails.append(f"{what}: «{m.group(0).strip()}» — "
                             f"только kind=legal формулировкой из словаря")

    if kind == "legal":
        term = match_legal_term(text)
        if term is None:
            fails.append("формулировки нет в словаре config/legal_terms.yaml")
        elif not str(term.get("case", "")).strip():
            fails.append("не названо конкретное дело")
        if term is not None and source_text and has_outcome(source_text) \
                and not term.get("outcome"):
            fails.append("в источнике есть исход дела, а в факте только стадия")

    # То же правило, что и для пересказа: факт не переписывает источник.
    if source_text:
        window = int(cfg("plagiarism_window_words", 10))
        if len(normalize_words(text)) >= window:
            match = longest_common_shingle(text, source_text, window)
            if match:
                fails.append(f"дословный кусок источника: «{match}»")

    if not (fact.get("quote") or "").strip():
        fails.append("пустая quote — факт непроверяем")

    topics = fact.get("topics") or []
    if not isinstance(topics, list) or not topics:
        fails.append("не указаны topics — сборщику нечем выбирать")

    return fails


# --------------------------------------------------------------- слой Б


def quote_found(quote: str, source_text: str) -> bool:
    """Есть ли цитата в источнике дословно.

    Сравниваем нормализованно: разница в кавычках, регистре и диакритике —
    не подделка, а типографика. Всё остальное считается несовпадением.
    Слишком короткая цитата не проверка, а совпадение общих слов.
    """
    needle = words(quote)
    if len(needle) < int(cfg("quote_min_words", 4)):
        return False
    return " ".join(needle) in " ".join(words(source_text))


def verify_quotes(facts: list[dict[str, Any]], source_text: str) -> list[dict[str, Any]]:
    """Вердикт слоя Б. Считает скрипт, а не критик.

    Два экземпляра одной модели склонны соглашаться друг с другом, поэтому
    ответ критика — сырьё для проверки, а не решение. Каждая цитата ищется
    в тексте; ненайденная опускает `found` в false независимо от того, что
    написал критик.
    """
    out = []
    for f in facts:
        found = quote_found(f.get("quote") or "", source_text)
        out.append({**f, "found": found})
        if not found:
            log.info("Цитата не найдена в источнике, факт отброшен: «%s»",
                     (f.get("fact") or "")[:70])
    return out


# --------------------------------------------------------------- извлечение


def _llm_json(system: str, user: str, usage: Any) -> dict[str, Any]:
    from .llm import json_call

    return json_call(system, user, usage, retries=1)


def extract(entity: dict[str, Any], source: dict[str, Any], usage: Any,
            known: list[str] | None = None) -> list[dict[str, Any]]:
    """Извлекатель: атомарные утверждения из текста источника.

    Не пишет связный текст и не составляет справку. Единственный источник
    фактов — переданный текст; всё, для чего не найдётся цитата, будет
    отброшено скриптом на слое Б.

    Уже принятые факты передаются, чтобы четыре источника об одном человеке
    не дали четыре варианта одной и той же должности: сборщик выбирает два
    факта, и если оба об одном, строка дважды сообщает одно.
    """
    tier = source.get("tier", "wiki")
    allowed = TIER_ALLOWS.get(tier, ())
    if not allowed:
        return []

    terms = "\n".join(
        f"- {t['es']} → {t['ru']}" for t in legal_terms()
    ) if "legal" in allowed else "(для этого уровня источника не разрешено)"

    already = ""
    if known:
        already = ("\nУже проверено и лежит в пуле — не повторяй это ни своими "
                   "словами, ни в другой формулировке:\n"
                   + "\n".join(f"- {k}" for k in known[:20]) + "\n")

    user = (
        f"Сущность: {entity.get('name_ru') or entity['name_es']} "
        f"({entity['name_es']}), тип: {entity.get('type', 'other')}\n"
        f"Уровень источника: {tier}\n"
        f"Разрешённые kind для этого уровня: {', '.join(allowed)}\n"
        f"Допустимые темы: {', '.join(TOPICS)}\n"
        f"Сколько фактов: от {int(cfg('min_facts', 2))} до {int(cfg('max_facts', 6))}\n"
        f"Словарь процессуальных формулировок:\n{terms}\n"
        f"{already}\n"
        f"Источник:\n---\n{source['text'][:int(cfg('max_source_chars', 6000))]}\n---"
    )
    data = _llm_json(load_prompt("fact_extractor.md"), user, usage)
    facts = data.get("facts") or []
    return [f for f in facts if isinstance(f, dict)]


def critic(facts: list[dict[str, Any]], source: dict[str, Any],
           usage: Any) -> list[dict[str, Any]]:
    """Критик ищет для каждого факта дословный фрагмент источника.

    Он не оценивает качество и не хвалит: у него один вопрос с проверяемым
    ответом. Проштамповать себе pass он не может — вердикт всё равно
    считает `verify_quotes`.
    """
    payload = [{"i": i, "fact": f.get("fact", ""), "quote": f.get("quote", "")}
               for i, f in enumerate(facts)]
    user = (
        f"Факты:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        f"Источник:\n---\n{source['text'][:int(cfg('max_source_chars', 6000))]}\n---"
    )
    try:
        data = _llm_json(load_prompt("fact_critic.md"), user, usage)
    except Exception as exc:  # noqa: BLE001 — критик не решает, он уточняет
        log.warning("Критик не ответил, оставляем цитаты извлекателя: %s", exc)
        return facts

    fixed = {int(item["i"]): item for item in (data.get("checks") or [])
             if isinstance(item, dict) and str(item.get("i", "")).isdigit()}
    out = []
    for i, f in enumerate(facts):
        found = fixed.get(i) or {}
        quote = (found.get("quote") or "").strip()
        out.append({**f, "quote": quote or f.get("quote", "")})
    return out


def run_extraction(entity: dict[str, Any], source: dict[str, Any], usage: Any,
                   known: list[str] | None = None) -> dict[str, Any]:
    """Полный цикл извлечения по одному источнику.

    Лимит итераций — два. Не прошедшие факты отбрасываются, остальные из того
    же источника сохраняются: отбрасывание факта не блокирует сущность,
    а требование «или всё, или ничего» оставило бы её вовсе без пула.
    """
    tier = source.get("tier", "wiki")
    text = source.get("text") or ""
    iterations = int(cfg("extract_iterations", 2))

    kept: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()

    for attempt in range(iterations):
        try:
            raw = extract(entity, source, usage, known)
        except Exception as exc:  # noqa: BLE001 — один источник не роняет прогон
            log.warning("Извлечение из %s не удалось: %s", source.get("url"), exc)
            break
        if not raw:
            break

        raw = critic(raw, source, usage)
        for f in verify_quotes(raw, text):
            key = normalize(f.get("fact", ""))
            if not key or key in seen:
                continue
            seen.add(key)
            problems = validate_fact(f, text, tier)
            if not f.get("found"):
                problems.append("цитата не найдена в источнике")
            if problems:
                rejected.append({"fact": f.get("fact"), "kind": f.get("kind"),
                                 "failures": problems})
                continue
            kept.append(f)

        if kept or attempt == iterations - 1:
            break
        log.info("Итерация %s: ни одного факта не прошло, пробуем ещё раз", attempt + 1)

    for r in rejected:
        log.info("Факт отклонён: «%s» — %s", (r["fact"] or "")[:60],
                 "; ".join(r["failures"][:2]))
    return {"kept": kept, "rejected": rejected, "tier": tier,
            "url": source.get("url"), "bucket": source.get("bucket", "")}


# --------------------------------------------------------------- пул


def save_facts(conn, entity_id: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    """Кладёт прошедшие факты в пул.

    Характеристика от одного полюса не становится активной сразу: она ждёт
    источника с другой стороны (см. `reconcile`). Позиция лагеря — не
    описание сущности, и в пул она попасть не должна.
    """
    from datetime import datetime, timedelta, timezone

    tier = result["tier"]
    bucket = result.get("bucket") or ""
    ttl = int(cfg("legal_ttl_days", 30))
    now = datetime.now(timezone.utc)

    saved: list[dict[str, Any]] = []
    for f in result["kept"]:
        kind = f["kind"]
        expires = now + timedelta(days=ttl) if kind == "legal" else None
        # оценка и классификация из прессы ждут подтверждения с другого полюса
        status = "candidate" if (tier == "press"
                                 and kind in ("evaluative", "classification")) else "active"
        # Повторная проверка того же утверждения продлевает ему срок, а не
        # плодит вторую запись. Отправленное в отставку владельцем не
        # воскресает: `fact retire` — решение человека, и переизвлечение
        # его не отменяет.
        row = conn.execute(
            """
            INSERT INTO entity_facts
                (entity_id, fact, kind, topics, source_url, source_tier,
                 source_bucket, quote, attribution, verified_at, expires_at, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s,%s)
            ON CONFLICT (entity_id, lower(btrim(fact))) DO UPDATE SET
                kind = EXCLUDED.kind,
                topics = EXCLUDED.topics,
                source_url = EXCLUDED.source_url,
                source_tier = EXCLUDED.source_tier,
                source_bucket = EXCLUDED.source_bucket,
                quote = EXCLUDED.quote,
                attribution = EXCLUDED.attribution,
                verified_at = now(),
                expires_at = EXCLUDED.expires_at,
                status = EXCLUDED.status
            WHERE entity_facts.status <> 'retired'
            RETURNING id, kind
            """,
            (entity_id, f["fact"].strip(), kind,
             [t for t in (f.get("topics") or []) if t in TOPICS],
             result.get("url"), tier, bucket, (f.get("quote") or "").strip(),
             (f.get("attribution") or "").strip(), expires, status),
        ).fetchone()
        if row:
            saved.append(dict(row))
    conn.execute("UPDATE entities SET facts_updated_at = now() WHERE id = %s",
                 (entity_id,))
    return saved


def retire_old_legal(conn, entity_id: str, keep_ids: list[int]) -> int:
    """Снимает прежний процессуальный статус после переизвлечения.

    Статус меняется по датам заседаний, и старая запись остаётся верной
    формально и ложной по сути: «фигурирует в расследовании» про человека,
    которого вчера оправдали, — худшая ошибка в системе.
    """
    rows = conn.execute(
        """
        UPDATE entity_facts SET status = 'retired'
        WHERE entity_id = %s AND kind = 'legal' AND status <> 'retired'
          AND NOT (id = ANY(%s))
        RETURNING id
        """,
        (entity_id, keep_ids or [0]),
    ).fetchall()
    return len(rows)


def drop_duplicates(kept: list[dict[str, Any]], known: list[str]
                    ) -> list[dict[str, Any]]:
    """Убирает факты, повторяющие уже принятые.

    Четыре источника об одном человеке дают «Председатель ACS», «Президент
    компании ACS» и «Председатель строительной компании ACS» — три записи
    об одном и том же. Сборщик потом выберет две из них, и читатель получит
    строку, дважды сообщающую одно. Точное совпадение ловит уникальный
    индекс, близкое — только сравнение по основам.
    """
    threshold = float(cfg("duplicate_similarity", 0.7))
    out: list[dict[str, Any]] = []
    seen = list(known)
    for f in kept:
        text = f.get("fact", "")
        if any(_similar(text, s) >= threshold for s in seen):
            log.info("Факт отброшен как повтор: «%s»", text[:70])
            continue
        seen.append(text)
        out.append(f)
    return out


def refresh(entity: dict[str, Any], *, dry_run: bool = True,
            with_press: bool = True) -> dict[str, Any]:
    """Пересобирает пул сущности по всей лестнице источников.

    Один источник, не давший ни одного факта, не останавливает остальные:
    у половины сущностей своей статьи нет, и пул набирается по кускам.
    """
    from .db import connect
    from .llm import LLMUsage
    from .sourcing import ladder

    usage = LLMUsage()
    stats: dict[str, Any] = {"entity_id": entity["id"], "sources": 0, "kept": 0,
                             "rejected": 0, "saved": 0, "duplicates": 0,
                             "facts": [], "cost_usd": 0.0}

    with connect() as conn:
        sources = ladder(conn, entity, with_press=with_press)
        # то, что уже лежит в пуле, тоже участвует в проверке на повтор
        known = [f["fact"] for f in pool(conn, entity["id"],
                                        statuses=("active", "candidate"),
                                        include_expired=True)]
    stats["sources"] = len(sources)
    if not sources:
        stats["reason"] = "источников не нашлось — остаётся только role_gloss"
        return stats

    legal_ids: list[int] = []
    for source in sources:
        result = run_extraction(entity, source, usage, known)
        stats["rejected"] += len(result["rejected"])

        fresh = drop_duplicates(result["kept"], known)
        stats["duplicates"] += len(result["kept"]) - len(fresh)
        known.extend(f["fact"] for f in fresh)
        result["kept"] = fresh

        stats["kept"] += len(fresh)
        stats["facts"].extend(fresh)
        if dry_run or not fresh:
            continue
        with connect() as conn:
            saved = save_facts(conn, entity["id"], result)
        stats["saved"] += len(saved)
        legal_ids.extend(f["id"] for f in saved if f["kind"] == "legal")

    if not dry_run:
        with connect() as conn:
            if legal_ids:
                stats["legal_retired"] = retire_old_legal(conn, entity["id"], legal_ids)
            stats["cross"] = reconcile(conn, entity["id"])

    stats["cost_usd"] = round(usage.cost_usd, 4)
    log.info("Пул %s: источников %s, принято %s, отклонено %s, повторов %s, $%.4f",
             entity["id"], stats["sources"], stats["kept"], stats["rejected"],
             stats["duplicates"], usage.cost_usd)
    return stats


def pool(conn, entity_id: str, *, statuses: tuple[str, ...] = ("active",),
         include_expired: bool = False) -> list[dict[str, Any]]:
    """Факты сущности, годные к показу.

    Просроченный `legal` не показывается вовсе: формулировок вида «на момент
    публикации» не существует, молчание безопаснее устаревшего обвинения.
    """
    sql = """
        SELECT * FROM entity_facts
        WHERE entity_id = %s AND status = ANY(%s)
    """
    if not include_expired:
        sql += " AND (expires_at IS NULL OR expires_at > now())"
    sql += " ORDER BY id"
    return [dict(r) for r in conn.execute(sql, (entity_id, list(statuses))).fetchall()]


def has_pool(conn, entity_id: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM entity_facts
        WHERE entity_id = %s AND status = 'active'
          AND (expires_at IS NULL OR expires_at > now())
        LIMIT 1
        """,
        (entity_id,),
    ).fetchone()
    return row is not None


def expire_legal(conn) -> list[str]:
    """Помечает просроченные процессуальные факты и возвращает их сущности.

    Просрочка — это не «показать с оговоркой», а молчание плюс очередь
    на перепроверку с приоритетом выше обычной регенерации.
    """
    rows = conn.execute(
        """
        UPDATE entity_facts SET status = 'stale'
        WHERE kind = 'legal' AND status = 'active'
          AND expires_at IS NOT NULL AND expires_at <= now()
        RETURNING entity_id
        """
    ).fetchall()
    return sorted({r["entity_id"] for r in rows})


def recheck_queue(conn, limit: int = 20) -> list[dict[str, Any]]:
    """Кого перепроверять в первую очередь.

    Сначала сущности с протухшим процессуальным статусом: устаревшее
    «фигурирует в расследовании» про оправданного человека — худший тип
    ошибки. Дальше — часто упоминаемые с давно не обновлявшимся пулом.
    """
    return [dict(r) for r in conn.execute(
        """
        SELECT e.*,
               EXISTS (SELECT 1 FROM entity_facts f
                       WHERE f.entity_id = e.id AND f.kind = 'legal'
                         AND f.status = 'stale') AS legal_stale
        FROM entities e
        WHERE NOT e.never_explain
        ORDER BY legal_stale DESC, e.facts_updated_at ASC NULLS FIRST,
                 e.mentions_count DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()]


# ------------------------------------------------- кросс-спектральное решение


def _similar(a: str, b: str) -> float:
    """Доля общих основ. Нужна, чтобы понять, об одном ли говорят два полюса."""
    wa = {w[:5] for w in normalize(a).split() if len(w) > 3}
    wb = {w[:5] for w in normalize(b).split() if len(w) > 3}
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


def reconcile(conn, entity_id: str) -> dict[str, int]:
    """Решает судьбу характеристик по тому, сходятся ли полюса (§4 спеки).

    Система не решает, кто прав. Она измеряет, сходятся ли источники разных
    политических бакетов:

    - одинаково у разных бакетов -> устоявшаяся классификация, атрибуция
      не нужна;
    - разные бакеты расходятся -> спорная характеристика, остаётся
      evaluative с именной атрибуцией, обе стороны отдельными фактами;
    - характеристика есть только у одного бакета -> в пул не попадает вовсе.
      Это позиция лагеря, а не описание сущности.
    """
    threshold = float(cfg("cross_similarity", 0.5))
    cands = pool(conn, entity_id, statuses=("candidate",), include_expired=True)
    stats = {"classified": 0, "activated": 0, "held": 0}
    if not cands:
        return stats

    buckets = {c["source_bucket"] for c in cands if c["source_bucket"]}
    if len(buckets) < 2:
        stats["held"] = len(cands)
        return stats

    promoted: set[int] = set()
    for i, a in enumerate(cands):
        for b in cands[i + 1:]:
            if not a["source_bucket"] or a["source_bucket"] == b["source_bucket"]:
                continue
            if _similar(a["fact"], b["fact"]) < threshold:
                continue
            # полюса говорят одно и то же — это уже не оценка, а классификация
            for f in (a, b):
                if f["id"] in promoted:
                    continue
                conn.execute(
                    "UPDATE entity_facts SET status='active', kind='classification' "
                    "WHERE id = %s", (f["id"],),
                )
                promoted.add(f["id"])
                stats["classified"] += 1

    # Полюса представлены оба, но об одном и том же не говорят: значит,
    # характеристика спорная. Она остаётся оценкой и выходит с именем того,
    # кто её высказал.
    for f in cands:
        if f["id"] in promoted or f["kind"] != "evaluative" or not f["attribution"]:
            continue
        conn.execute("UPDATE entity_facts SET status='active' WHERE id = %s", (f["id"],))
        stats["activated"] += 1

    log.info("Сущность %s: классификаций %s, спорных оценок %s, придержано %s",
             entity_id, stats["classified"], stats["activated"], stats["held"])
    return stats


# --------------------------------------------------------------- сборка


# Приоритет при равном совпадении по теме. Роль отвечает на «кто это» и
# нужна всегда; масштаб и классификация — уточнение.
_KIND_ORDER = {"role": 0, "classification": 1, "scale": 2, "legal": 3, "evaluative": 4}


def select_facts(facts: list[dict[str, Any]], topic: str,
                 limit: int | None = None) -> list[dict[str, Any]]:
    """Какие факты уместны в посте на эту тему.

    Совпадение по `topics` — главный критерий; при равенстве работает
    приоритет типов. `evaluative` и `legal` берутся, только если тема поста
    совпала с областью характеристики или относится к делу: характеристика
    в чужом сюжете — это ярлык, приклеенный к человеку заодно.
    """
    limit = int(cfg("facts_per_context", 2)) if limit is None else limit
    topic = (topic or "").strip().lower()

    scored: list[tuple[int, int, int, dict[str, Any]]] = []
    for f in facts:
        topics = [str(t).lower() for t in (f.get("topics") or [])]
        on_topic = topic in topics if topic else False
        if f["kind"] in ("evaluative", "legal") and not on_topic:
            continue
        scored.append((0 if on_topic else 1, _KIND_ORDER.get(f["kind"], 9),
                       f.get("id") or 0, f))

    scored.sort(key=lambda x: (x[0], x[1], x[2]))
    return [f for *_, f in scored[:limit]]


def validate_assembly(context: str, facts: list[dict[str, Any]]) -> dict[str, Any]:
    """Проверяет, что сборщик не внёс информации, которой нет в фактах.

    Сравнение по основам: согласование падежей — разрешённая операция, и
    считать «председателя» новым словом относительно «председатель» было бы
    неверно. Всё, что не сводится к основе из фактов и не служебное, —
    внесённое утверждение, и такая сборка отбрасывается целиком.
    """
    limit = int(cfg("max_context_chars", 200))
    context = (context or "").strip()
    if not context:
        # пустая сборка — нормальный результат: подходящих фактов не нашлось
        return {"passed": True, "foreign": [], "length": 0, "failures": []}

    fails: list[str] = []
    if len(context) > limit:
        fails.append(f"длина {len(context)} при пределе {limit}")

    known: set[str] = set()
    for f in facts:
        for word in normalize(f.get("fact", "")).split():
            known.add(word[:5])
        # атрибуция сохраняется дословно, её слова легальны
        for word in normalize(f.get("attribution", "")).split():
            known.add(word[:5])

    stop = set(load("stopwords_ru.txt"))
    foreign = sorted({
        w for w in normalize(context).split()
        if len(w) > 2 and not w.isdigit() and w not in stop and w[:5] not in known
    })
    if foreign:
        fails.append("посторонние слова: " + ", ".join(foreign[:5]))

    return {"passed": not fails, "foreign": foreign,
            "length": len(context), "failures": fails}


def assemble(entity: dict[str, Any], topic: str, headline: str,
             facts: list[dict[str, Any]], usage: Any) -> dict[str, Any]:
    """Сборщик: строка «кто это» под конкретный пост.

    Источников он не видит вовсе — это структурная гарантия того, что
    написать он может только уже проверенное. Разрешены выбор, соединение
    и согласование; добавлять нельзя.
    """
    payload = [
        {"id": f["id"], "fact": f["fact"], "kind": f["kind"],
         "topics": list(f.get("topics") or []), "attribution": f.get("attribution", "")}
        for f in facts
    ]
    user = (
        f"Сущность: {entity.get('name_ru') or entity['name_es']}\n"
        f"Тема поста: {topic}\n"
        f"Заголовок поста: {headline}\n"
        f"Предел длины: {int(cfg('max_context_chars', 200))} символов\n\n"
        f"Доступные факты:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )
    data = _llm_json(load_prompt("fact_assembler.md"), user, usage)
    return {
        "context": (data.get("context") or "").strip(),
        "fact_ids": [int(i) for i in (data.get("used_fact_ids") or [])
                     if str(i).isdigit()],
    }


def build_context(conn, entity: dict[str, Any], topic: str, headline: str,
                  usage: Any) -> dict[str, Any] | None:
    """Собранный контекст для одной сущности в одном посте.

    None — контекста не будет: подходящих фактов нет или сборка не прошла
    проверку. Это штатный исход, а не сбой: читателя держит role_gloss
    в теле поста, и пустой блок лучше выдуманного.
    """
    if entity.get("never_explain"):
        return None

    facts = select_facts(pool(conn, entity["id"]), topic)
    if not facts:
        return None

    try:
        out = assemble(entity, topic, headline, facts, usage)
    except Exception as exc:  # noqa: BLE001 — одна сборка не роняет пост
        log.warning("Контекст для %s не собрался: %s", entity["id"], exc)
        return None

    used = [f for f in facts if f["id"] in out["fact_ids"]] or facts
    check = validate_assembly(out["context"], used)
    if not check["passed"]:
        log.warning("Сборка для %s отброшена: %s", entity["id"],
                    "; ".join(check["failures"]))
        return None
    if not out["context"]:
        return None

    return {
        "context": out["context"],
        "fact_ids": [f["id"] for f in used],
        # ссылка на источник показанных фактов: тексты Википедии под CC BY-SA,
        # и ведёт она туда, откуда взято показанное, а не в общую статью
        "url": next((f.get("source_url") for f in used if f.get("source_url")), ""),
    }


def contexts_for(conn, entities: list[dict[str, Any]], topic: str, headline: str,
                 usage: Any) -> dict[str, dict[str, Any]]:
    """Собранный контекст для всех сущностей поста.

    Сущности, для которых контекст не собрался, в результат не попадают —
    и в блоке их не будет. Это не ошибка: пустой блок лучше выдуманного,
    а имя читателю уже пояснено ролью в теле поста.
    """
    out: dict[str, dict[str, Any]] = {}
    for e in entities:
        built = build_context(conn, e, topic, headline, usage)
        if built:
            out[e["id"]] = built
    return out
