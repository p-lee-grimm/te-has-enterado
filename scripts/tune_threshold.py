#!/usr/bin/env python
"""Калибровка cluster.sim_threshold по накопленному корпусу.

Порог 0.82 в спеке — догадка. Этот скрипт прогоняет тот же онлайн-алгоритм
в памяти для сетки порогов и показывает, что получается. Решение принимает
владелец глазами, автоматического выбора здесь нет и быть не должно.

    python scripts/tune_threshold.py
    python scripts/tune_threshold.py --from 0.50 --to 0.80 --step 0.02
    python scripts/tune_threshold.py --pairs-at 0.82
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from quepasa.config import get_settings  # noqa: E402
from quepasa.db import connect  # noqa: E402

console = Console(width=int(os.environ.get("QP_TABLE_WIDTH", "150")))


def load_corpus(days: int, limit: int):
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.title, a.source_id, a.published_at, a.embedding
            FROM articles a
            WHERE a.embedding IS NOT NULL
              AND a.published_at >= now() - make_interval(days => %s)
            ORDER BY a.published_at ASC, a.id ASC
            LIMIT %s
            """,
            (days, limit),
        ).fetchall()
    return rows


def cluster_offline(rows, threshold: float, window_hours: int):
    """Тот же алгоритм, что в стадии cluster, но в памяти и без записи.

    Центроиды держим одной матрицей и сравниваем со статьёй за один
    матричный умножитель. Наивный цикл по кластерам на корпусе в тысячи
    статей считает миллиарды пар и не доживает до конца сетки.

    Возвращает список кластеров: {'members': [idx]}.
    """
    n = len(rows)
    dim = len(rows[0]["embedding"])
    cent = np.zeros((n, dim), dtype=np.float32)
    norms = np.ones(n, dtype=np.float32)
    last = np.full(n, -1e18, dtype=np.float64)
    members: list[list[int]] = []
    window = window_hours * 3600.0

    for i, row in enumerate(rows):
        vec = np.asarray(row["embedding"], dtype=np.float32)
        nv = float(np.linalg.norm(vec)) or 1.0
        ts = row["published_at"].timestamp()
        k = len(members)

        best_j, best_sim = -1, -1.0
        if k:
            sims = (cent[:k] @ vec) / (norms[:k] * nv)
            # окно: кластер, молчавший дольше window, для новой статьи закрыт
            sims = np.where(ts - last[:k] <= window, sims, -1.0)
            best_j = int(np.argmax(sims))
            best_sim = float(sims[best_j])

        if best_j >= 0 and best_sim >= threshold:
            members[best_j].append(i)
            cnt = len(members[best_j])
            cent[best_j] += (vec - cent[best_j]) / cnt
            norms[best_j] = float(np.linalg.norm(cent[best_j])) or 1.0
            last[best_j] = max(last[best_j], ts)
        else:
            cent[k] = vec
            norms[k] = nv
            last[k] = ts
            members.append([i])
    return [{"members": m} for m in members]


def summarise(rows, clusters, threshold: float) -> dict:
    sizes = sorted((len(c["members"]) for c in clusters), reverse=True)
    multi_source = 0
    for c in clusters:
        srcs = {rows[i]["source_id"] for i in c["members"]}
        if len(srcs) >= 3:
            multi_source += 1
    return {
        "threshold": threshold,
        "clusters": len(clusters),
        "singletons": sum(1 for s in sizes if s == 1),
        "top1": sizes[0] if sizes else 0,
        "top5": sizes[:5],
        "median": sizes[len(sizes) // 2] if sizes else 0,
        # ровно те кластеры, которые вообще могут попасть в выпуск (§3.7)
        "eligible": multi_source,
    }


def border_pairs(
    rows, threshold: float, window_hours: int, n: int, band: float,
    cross_source_only: bool = True,
):
    """Пары «на границе»: сходство в узкой полосе вокруг порога.

    Их читает человек и решает, один это сюжет или разные.

    По умолчанию берём только пары из РАЗНЫХ изданий: порог решает именно вопрос
    «пишут ли двое об одном событии». Пары внутри одного источника (особенно BOE
    с его шаблонными формулировками) забивают выборку и ничего не говорят.
    """
    vecs = [np.asarray(r["embedding"], dtype=np.float32) for r in rows]
    norms = [float(np.linalg.norm(v)) or 1.0 for v in vecs]

    idx = list(range(len(rows)))
    random.shuffle(idx)
    found: list[tuple[float, int, int]] = []
    # не перебираем все пары — на корпусе в тысячи статей это лишнее
    budget = 400_000
    tried = 0
    for a_pos, i in enumerate(idx):
        for j in idx[a_pos + 1 :]:
            tried += 1
            if tried > budget or len(found) >= n * 4:
                break
            if cross_source_only and rows[i]["source_id"] == rows[j]["source_id"]:
                continue
            dt = abs((rows[i]["published_at"] - rows[j]["published_at"]).total_seconds())
            if dt > window_hours * 3600:
                continue
            sim = float(np.dot(vecs[i], vecs[j])) / (norms[i] * norms[j])
            if abs(sim - threshold) <= band:
                found.append((sim, i, j))
        if tried > budget or len(found) >= n * 4:
            break

    random.shuffle(found)
    return sorted(found[:n], key=lambda t: -t[0])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="lo", type=float, default=0.74)
    ap.add_argument("--to", dest="hi", type=float, default=0.90)
    ap.add_argument("--step", type=float, default=0.02)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--pairs", type=int, default=20, help="сколько пограничных пар показать")
    ap.add_argument("--pairs-at", type=float, help="порог, вокруг которого искать пары")
    ap.add_argument("--band", type=float, default=0.03)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--include-same-source", action="store_true",
                    help="показывать и пары внутри одного издания (по умолчанию только разные)")
    args = ap.parse_args()

    random.seed(args.seed)
    s = get_settings()
    window = int(s.require("cluster.window_hours"))
    current = float(s.require("cluster.sim_threshold"))

    rows = load_corpus(args.days, args.limit)
    if len(rows) < 50:
        console.print(
            f"[red]В корпусе всего {len(rows)} статей с эмбеддингами. "
            "Калибровать рано — дай ingest поработать пару дней (§8.2).[/]"
        )
        return 1

    console.print(
        f"Корпус: [bold]{len(rows)}[/] статей за {args.days} дн., "
        f"окно {window} ч, провайдер [bold]{s.require('embed.provider')}[/] "
        f"({s.require('embed.model')}), текущий порог {current}\n"
    )

    table = Table(title="Сетка порогов")
    table.add_column("порог", justify="right")
    table.add_column("кластеров", justify="right")
    table.add_column("одиночек", justify="right")
    table.add_column("медиана", justify="right")
    table.add_column("крупнейший", justify="right")
    table.add_column("топ-5 размеров")
    table.add_column("годных\n(≥3 источн.)", justify="right")

    t = args.lo
    while t <= args.hi + 1e-9:
        res = summarise(rows, cluster_offline(rows, t, window), t)
        mark = " ←" if abs(t - current) < 1e-9 else ""
        table.add_row(
            f"{t:.2f}{mark}",
            str(res["clusters"]),
            str(res["singletons"]),
            str(res["median"]),
            str(res["top1"]),
            ", ".join(map(str, res["top5"])),
            f"[bold]{res['eligible']}[/]",
        )
        t += args.step
    console.print(table)

    at = args.pairs_at if args.pairs_at is not None else current
    console.print(
        f"\n[bold]Пары на границе порога {at:.2f} (±{args.band})[/] — "
        "читать глазами: один это сюжет или разные?\n"
    )
    pairs = border_pairs(rows, at, window, args.pairs, args.band,
                         cross_source_only=not args.include_same_source)
    if not pairs:
        console.print("[yellow]Пар в этой полосе не нашлось. Попробуй --band пошире.[/]")
    for k, (sim, i, j) in enumerate(pairs, 1):
        same = "ОДИН ИСТОЧНИК" if rows[i]["source_id"] == rows[j]["source_id"] else ""
        console.print(f"[bold]{k:2}. sim={sim:.3f}[/] {same}")
        console.print(f"    [dim]{rows[i]['source_id']:14}[/] {rows[i]['title'][:100]}")
        console.print(f"    [dim]{rows[j]['source_id']:14}[/] {rows[j]['title'][:100]}\n")

    console.print(
        "Порог правится вручную в config/settings.yaml -> cluster.sim_threshold.\n"
        "Ориентир: растёт «годных» при том, что пары на границе всё ещё читаются "
        "как один сюжет."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
