#!/usr/bin/env python
"""Статистика по сюжетам — чтобы задать правило автопостинга по фактам.

Главный вопрос правила не «сколько источников», а «сколько ждать»: издания
пишут об одном событии не одновременно, и порог в N источников набирается
не мгновенно. Поэтому здесь есть распределение времени до N-го источника.

    python scripts/stats.py
    python scripts/stats.py --day 2026-08-10
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from quepasa.db import connect  # noqa: E402

console = Console(width=int(os.environ.get("QP_TABLE_WIDTH", "104")))

LEFT = ("far-left", "left")
RIGHT = ("right", "far-right")
LEFT_BROAD = ("far-left", "left", "center-left")
RIGHT_BROAD = ("center-right", "right", "far-right")


def section(title: str) -> None:
    console.print(f"\n[bold]{title}[/]")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day", help="YYYY-MM-DD; по умолчанию — все данные")
    args = ap.parse_args()

    where = "AND a.published_at::date = %(day)s" if args.day else ""
    params = {"day": args.day} if args.day else {}

    with connect() as conn:
        period = conn.execute(
            "SELECT min(published_at) AS lo, max(published_at) AS hi, count(*) AS n "
            "FROM articles"
        ).fetchone()

        # сводка по каждому сюжету: сколько источников, какие полюса, тайминги
        rows = conn.execute(
            f"""
            SELECT c.id,
                   count(DISTINCT a.source_id)                    AS n_sources,
                   count(*)                                       AS n_articles,
                   array_agg(DISTINCT s.lean)                     AS leans,
                   bool_or(s.type = 'agency')                     AS has_agency,
                   bool_or(s.type = 'official')                   AS has_official,
                   min(a.published_at)                            AS first_at,
                   max(a.published_at)                            AS last_at
            FROM clusters c
            JOIN articles a ON a.cluster_id = c.id
            JOIN sources s  ON s.id = a.source_id
            WHERE TRUE {where}
            GROUP BY c.id
            """,
            params,
        ).fetchall()

        # когда в сюжет пришёл 2-й, 3-й, 5-й РАЗНЫЙ источник
        timing = conn.execute(
            f"""
            WITH firsts AS (
                SELECT a.cluster_id, a.source_id, min(a.published_at) AS at
                FROM articles a JOIN sources s ON s.id = a.source_id
                WHERE a.cluster_id IS NOT NULL {where}
                GROUP BY a.cluster_id, a.source_id
            ), ranked AS (
                SELECT cluster_id, at,
                       row_number() OVER (PARTITION BY cluster_id ORDER BY at) AS k,
                       min(at)     OVER (PARTITION BY cluster_id)              AS t0
                FROM firsts
            )
            SELECT k, count(*) AS n,
                   percentile_disc(0.5) WITHIN GROUP (
                       ORDER BY EXTRACT(EPOCH FROM (at - t0))/3600) AS median_h,
                   percentile_disc(0.9) WITHIN GROUP (
                       ORDER BY EXTRACT(EPOCH FROM (at - t0))/3600) AS p90_h
            FROM ranked WHERE k IN (2,3,5) GROUP BY k ORDER BY k
            """,
            params,
        ).fetchall()

        by_day = conn.execute(
            """
            SELECT a.published_at::date AS d,
                   count(DISTINCT a.cluster_id) AS clusters,
                   count(*) AS articles
            FROM articles a WHERE a.cluster_id IS NOT NULL
            GROUP BY 1 ORDER BY 1
            """
        ).fetchall()

        top_sources = conn.execute(
            """
            SELECT s.name, s.lean,
                   count(DISTINCT a.cluster_id) FILTER (
                       WHERE c.n_sources >= 3) AS in_big
            FROM sources s
            JOIN articles a ON a.source_id = s.id
            JOIN clusters c ON c.id = a.cluster_id
            GROUP BY s.name, s.lean ORDER BY in_big DESC LIMIT 8
            """
        ).fetchall()

    console.print(
        f"Период: [bold]{period['lo']:%d.%m %H:%M} — {period['hi']:%d.%m %H:%M}[/] "
        f"({period['n']} статей). "
        f"{'День: ' + args.day if args.day else 'Все данные'}"
    )

    # ---------------------------------------------------------------- объём
    section("Сколько сюжетов набирается")
    t = Table()
    t.add_column("порог")
    t.add_column("сюжетов", justify="right")
    t.add_column("доля", justify="right")
    total = len(rows)
    for label, cond in [
        ("всего сюжетов", lambda r: True),
        ("≥2 источника", lambda r: r["n_sources"] >= 2),
        ("≥3 источника", lambda r: r["n_sources"] >= 3),
        ("≥5 источников", lambda r: r["n_sources"] >= 5),
        ("≥3 и есть агентство", lambda r: r["n_sources"] >= 3 and r["has_agency"]),
    ]:
        n = sum(1 for r in rows if cond(r))
        t.add_row(label, str(n), f"{100 * n / total:.1f}%" if total else "—")
    console.print(t)

    # ---------------------------------------------------------------- полюса
    section("Разброс по политическому спектру")
    t = Table()
    t.add_column("условие")
    t.add_column("сюжетов", justify="right")
    t.add_column("из них ≥3 источн.", justify="right")

    def has(r, group):
        return any(x in group for x in r["leans"])

    checks = [
        ("есть и левое, и правое (строго)",
         lambda r: has(r, LEFT) and has(r, RIGHT)),
        ("есть и левое, и правое (с центристскими)",
         lambda r: has(r, LEFT_BROAD) and has(r, RIGHT_BROAD)),
        ("≥3 разных полюса", lambda r: len(set(r["leans"])) >= 3),
        ("только один полюс", lambda r: len(set(r["leans"])) == 1),
    ]
    for label, cond in checks:
        sel = [r for r in rows if cond(r)]
        t.add_row(label, str(len(sel)), str(sum(1 for r in sel if r["n_sources"] >= 3)))
    console.print(t)

    # ---------------------------------------------------------------- тайминг
    section("Сколько ждать: время от первой публикации до N-го источника")
    t = Table()
    t.add_column("источник")
    t.add_column("сюжетов дошло", justify="right")
    t.add_column("медиана", justify="right")
    t.add_column("90% укладываются в", justify="right")
    names = {2: "2-й", 3: "3-й", 5: "5-й"}
    for r in timing:
        t.add_row(
            names.get(int(r["k"]), str(r["k"])),
            str(r["n"]),
            f"{float(r['median_h']):.1f} ч",
            f"{float(r['p90_h']):.1f} ч",
        )
    console.print(t)
    console.print(
        "[dim]Это и есть цена правила: постишь по достижении 3 источников — "
        "ждёшь примерно медиану; постишь раньше — чаще правишь пост.[/]"
    )

    # ------------------------------------------------------- дорост после 3
    section("Как часто пост придётся дополнять")
    reach3 = [r for r in rows if r["n_sources"] >= 3]
    grew = [r for r in reach3 if r["n_sources"] >= 4]
    grew5 = [r for r in reach3 if r["n_sources"] >= 5]
    console.print(
        f"  Из {len(reach3)} сюжетов с ≥3 источниками "
        f"{len(grew)} ({100 * len(grew) / max(1, len(reach3)):.0f}%) "
        f"добрали 4-й, {len(grew5)} ({100 * len(grew5) / max(1, len(reach3)):.0f}%) — 5-й.\n"
        "  [dim]То есть примерно столько постов надо будет править после публикации.[/]"
    )

    # ---------------------------------------------------------------- по дням
    section("По дням")
    t = Table()
    t.add_column("дата")
    t.add_column("статей", justify="right")
    t.add_column("сюжетов", justify="right")
    for r in by_day:
        t.add_row(str(r["d"]), str(r["articles"]), str(r["clusters"]))
    console.print(t)

    section("Кто чаще попадает в крупные сюжеты")
    t = Table()
    t.add_column("издание")
    t.add_column("полюс")
    t.add_column("сюжетов ≥3 ист.", justify="right")
    for r in top_sources:
        t.add_row(r["name"], r["lean"], str(r["in_big"]))
    console.print(t)

    console.print(
        "\n[dim]Категорий здесь нет: тему определяет модель на этапе пересказа, "
        "а он ещё не прогонялся. После первых постов статистика по категориям "
        "появится в таблице posts.[/]"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
