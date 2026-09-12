#!/usr/bin/env python
"""Выборка для калибровки классификатора значимости.

Промпт настраивается на своей базе, а не на воображении: без размеченной
выборки правки формулировок не проверяются, и промпт незаметно подгоняется
под те три примера, которые автор помнит.

Порядок работы:

    python scripts/impact_dataset.py export --limit 200 > labeled.jsonl
    # руками проставить label: keep | drop в каждой строке
    python scripts/impact_dataset.py classify --file labeled.jsonl
    python scripts/eval_impact.py --file labeled.jsonl --train

Треть выборки (каждая третья строка) отложена: она считается только
`--holdout` и только после того, как промпт перестал меняться. Иначе
отложенной части не остаётся — она тоже участвует в настройке.

Классификация идёт по снимку из файла, а не по базе: полные тексты живут
72 часа (§5.4), и через неделю тот же сюжет дал бы другой вход.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quepasa.config import get_settings, load_prompt  # noqa: E402
from quepasa.db import connect  # noqa: E402
from quepasa.impact import clean, user_message  # noqa: E402


def export(limit: int, days: int, min_sources: int) -> list[dict]:
    """Сохранённые сюжеты со всем, что видит классификатор.

    Выборка повторяет то, что доходит до классификатора в работе: ежедневные
    рубрики (лотереи, гороскопы) и светские разделы отсеиваются раньше него,
    и держать их в калибровочном наборе бессмысленно — промпт настроится на
    мусор, которого он никогда не увидит.
    """
    from quepasa.posts import is_junk
    from quepasa.stages.ingest import excluded_section
    from quepasa.textutil import url_sections

    s = get_settings()
    max_headlines = int(s.get_path("impact.max_headlines", 8))
    lead_chars = int(s.get_path("impact.lead_chars", 700))
    depth = int(s.get_path("fetch.section_url_depth", 2))
    excluded = {x.strip().lower() for x in s.get_path("fetch.exclude_sections", []) or []}

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT c.id,
                   count(DISTINCT a.source_id) AS n_sources,
                   max(a.published_at)         AS last_at,
                   array_agg(DISTINCT a.title)  AS titles,
                   array_agg(DISTINCT a.url)    AS urls,
                   array_remove(array_agg(DISTINCT a.section), NULL) AS sections
            FROM clusters c
            JOIN articles a ON a.cluster_id = c.id
            WHERE a.published_at >= now() - make_interval(days => %s)
            GROUP BY c.id
            HAVING count(DISTINCT a.source_id) >= %s
            ORDER BY max(a.published_at) DESC
            """,
            (days, min_sources),
        ).fetchall()

        picked = []
        for r in rows:
            if len(picked) >= limit:
                break
            titles = list(r["titles"])
            if is_junk(titles):
                continue
            # раздела у старых статей нет — добираем его из адреса
            secs = list(r["sections"]) or [
                x for u in (r["urls"] or []) for x in url_sections(u or "", depth)
            ]
            if excluded_section(secs, excluded):
                continue
            picked.append((r, titles, secs))

        out = []
        for r, titles, secs in picked:
            lead = conn.execute(
                """
                SELECT COALESCE(NULLIF(body, ''), summary_feed, '') AS lead
                FROM articles WHERE cluster_id = %s
                ORDER BY length(COALESCE(body, summary_feed, '')) DESC LIMIT 1
                """,
                (r["id"],),
            ).fetchone()
            out.append({
                "id": r["id"],
                "headline": titles[0] if titles else "",
                "headlines": titles[:max_headlines],
                "sections": sorted({x for x in secs if x})[:6],
                "lead": (lead["lead"] if lead else "")[:lead_chars],
                "n_sources": int(r["n_sources"]),
                "label": "",
            })
    # по id: деление на калибровочные две трети и отложенную треть в
    # eval_impact.py идёт по порядку строк, и он должен быть стабильным
    out.sort(key=lambda x: x["id"])
    return out


def snapshot_message(row: dict) -> str:
    return user_message({
        "headlines": "\n".join(f"- {t}" for t in row.get("headlines") or []),
        "sections": ", ".join(row.get("sections") or []) or "не определены",
        "lead": row.get("lead") or "",
    })


def save(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )


def load(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def classify_file(path: Path, force: bool, jobs: int) -> int:
    """Прогон классификатора по файлу. Сюжеты независимы, поэтому идут пачкой:
    двести последовательных вызовов — это полтора часа ожидания."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Lock

    from quepasa.llm import LLMUsage, json_call

    rows = load(path)
    system = load_prompt("impact.md")
    usage = LLMUsage()
    lock = Lock()  # счётчики usage общие на все потоки
    todo = [r for r in rows if force or not r.get("impact")]
    done = 0

    def one(row: dict) -> None:
        nonlocal done
        try:
            local = LLMUsage()
            verdict = clean(json_call(system, snapshot_message(row), local))
        except Exception as exc:  # noqa: BLE001 — один сюжет не роняет прогон
            print(f"  сюжет {row.get('id')}: {exc}", file=sys.stderr)
            return
        with lock:
            row.update(verdict)
            usage.tokens_in += local.tokens_in
            usage.tokens_out += local.tokens_out
            usage.reported_cost_usd += local.reported_cost_usd
            done += 1
            print(f"  [{done}/{len(todo)}] {row['impact']:14} "
                  f"{row.get('headline','')[:70]}", file=sys.stderr)
            # пишем по ходу: прогон на двести сюжетов длинный, и прерывать
            # его не должно быть страшно
            save(path, rows)

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        list(pool.map(one, todo))

    save(path, rows)
    print(f"классифицировано: {done}, стоимость: ${usage.cost_usd:.3f}", file=sys.stderr)
    return done


def label_file(path: Path, redo: bool) -> int:
    """Ручная разметка в терминале: k — оставить, d — выбросить.

    Размечает владелец, а не модель: разметка — это политика канала, и если
    её проставит та же модель, оценка будет мерить согласие модели с собой.
    """
    rows = load(path)
    todo = [r for r in rows if redo or r.get("label") not in ("keep", "drop")]
    if not todo:
        print("всё размечено", file=sys.stderr)
        return 0

    keys = {"k": "keep", "d": "drop"}
    done = 0
    for n, row in enumerate(todo, 1):
        print(f"\n[{n}/{len(todo)}] {row.get('headline','')}")
        for extra in (row.get("headlines") or [])[1:3]:
            print(f"        {extra}")
        if row.get("sections"):
            print(f"        разделы: {', '.join(row['sections'])}")
        while True:
            answer = input("  k — в канал, d — мимо, s — пропустить, q — выйти: ").strip().lower()
            if answer in keys or answer in ("s", "q", ""):
                break
        if answer == "q":
            break
        if answer in keys:
            row["label"] = keys[answer]
            done += 1
            save(path, rows)

    left = sum(1 for r in rows if r.get("label") not in ("keep", "drop"))
    print(f"\nразмечено за подход: {done}, осталось: {left}", file=sys.stderr)
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("export", help="выгрузить сюжеты в JSONL для разметки")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--min-sources", type=int, default=2)

    p = sub.add_parser("classify", help="проставить impact классификатором")
    p.add_argument("--file", required=True)
    p.add_argument("--force", action="store_true", help="пересчитать уже размеченные")
    p.add_argument("--jobs", type=int, default=4, help="сколько сюжетов считать разом")

    p = sub.add_parser("label", help="ручная разметка keep/drop в терминале")
    p.add_argument("--file", required=True)
    p.add_argument("--redo", action="store_true", help="переразметить уже размеченное")

    a = ap.parse_args()

    if a.cmd == "export":
        for row in export(a.limit, a.days, a.min_sources):
            print(json.dumps(row, ensure_ascii=False))
        return 0

    if a.cmd == "label":
        label_file(Path(a.file), a.redo)
        return 0

    classify_file(Path(a.file), a.force, a.jobs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
