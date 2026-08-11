"""Калибровка порога кластеризации: общий код для CLI и веб-консоли.

Здесь нет ничего, что решает за владельца. Скрипт и веб только показывают
последствия выбора; порог ставит человек.
"""

from __future__ import annotations

import random
from typing import Any

import numpy as np

from .db import connect


def load_corpus(days: int = 7, limit: int = 5000) -> list[dict[str, Any]]:
    with connect() as conn:
        return conn.execute(
            """
            SELECT a.id, a.title, a.url, a.source_id, a.published_at, a.embedding,
                   s.name AS source_name, s.lean
            FROM articles a
            JOIN sources s ON s.id = a.source_id
            WHERE a.embedding IS NOT NULL
              AND a.published_at >= now() - make_interval(days => %s)
            ORDER BY a.published_at ASC, a.id ASC
            LIMIT %s
            """,
            (days, limit),
        ).fetchall()


def cluster_offline(rows, threshold: float, window_hours: int) -> list[dict]:
    """Тот же онлайн-алгоритм, что в стадии cluster, но в памяти и без записи."""
    clusters: list[dict] = []
    for i, row in enumerate(rows):
        vec = np.asarray(row["embedding"], dtype=np.float32)
        ts = row["published_at"]

        best_j, best_sim = -1, -1.0
        for j, cl in enumerate(clusters):
            if (ts - cl["last"]).total_seconds() > window_hours * 3600:
                continue
            c = cl["centroid"]
            denom = float(np.linalg.norm(c) * np.linalg.norm(vec)) or 1.0
            sim = float(np.dot(c, vec)) / denom
            if sim > best_sim:
                best_j, best_sim = j, sim

        if best_j >= 0 and best_sim >= threshold:
            cl = clusters[best_j]
            cl["members"].append(i)
            cl["centroid"] = cl["centroid"] + (vec - cl["centroid"]) / len(cl["members"])
            cl["last"] = max(cl["last"], ts)
        else:
            clusters.append({"members": [i], "centroid": vec.copy(), "last": ts})
    return clusters


def summarise(rows, clusters, threshold: float) -> dict[str, Any]:
    sizes = sorted((len(c["members"]) for c in clusters), reverse=True)
    eligible = sum(
        1 for c in clusters if len({rows[i]["source_id"] for i in c["members"]}) >= 3
    )
    return {
        "threshold": round(threshold, 4),
        "clusters": len(clusters),
        "singletons": sum(1 for s in sizes if s == 1),
        "top1": sizes[0] if sizes else 0,
        "top5": sizes[:5],
        "median": sizes[len(sizes) // 2] if sizes else 0,
        "eligible": eligible,
    }


def sweep(rows, lo: float, hi: float, step: float, window_hours: int) -> list[dict]:
    out, t = [], lo
    while t <= hi + 1e-9:
        out.append(summarise(rows, cluster_offline(rows, t, window_hours), t))
        t += step
    return out


def similarity(rows, i: int, j: int) -> float:
    a = np.asarray(rows[i]["embedding"], dtype=np.float32)
    b = np.asarray(rows[j]["embedding"], dtype=np.float32)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b)) or 1.0
    return float(np.dot(a, b)) / denom


def border_pairs(
    rows, threshold: float, window_hours: int, n: int, band: float,
    cross_source_only: bool = True, exclude: set[tuple[int, int]] | None = None,
    seed: int | None = None,
) -> list[tuple[float, int, int]]:
    """Пары «на границе»: сходство в узкой полосе вокруг порога.

    По умолчанию только пары из РАЗНЫХ изданий: порог решает именно вопрос
    «пишут ли двое об одном событии». Пары внутри одного источника (особенно
    BOE с его шаблонными формулировками) забивают выборку и ничего не говорят.
    """
    if seed is not None:
        random.seed(seed)
    exclude = exclude or set()

    vecs = [np.asarray(r["embedding"], dtype=np.float32) for r in rows]
    norms = [float(np.linalg.norm(v)) or 1.0 for v in vecs]

    idx = list(range(len(rows)))
    random.shuffle(idx)
    found: list[tuple[float, int, int]] = []
    budget, tried = 400_000, 0

    for pos, i in enumerate(idx):
        for j in idx[pos + 1 :]:
            tried += 1
            if tried > budget or len(found) >= n * 4:
                break
            if cross_source_only and rows[i]["source_id"] == rows[j]["source_id"]:
                continue
            key = (min(rows[i]["id"], rows[j]["id"]), max(rows[i]["id"], rows[j]["id"]))
            if key in exclude:
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


# ------------------------------------------------------- разметка пар


def save_label(article_a: int, article_b: int, same: bool, sim: float) -> None:
    a, b = sorted((article_a, article_b))
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO pair_labels (article_a, article_b, same_story, sim)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (article_a, article_b)
            DO UPDATE SET same_story = EXCLUDED.same_story, sim = EXCLUDED.sim,
                          labelled_at = now()
            """,
            (a, b, same, sim),
        )


def labels() -> list[dict[str, Any]]:
    with connect() as conn:
        return conn.execute(
            "SELECT article_a, article_b, same_story, sim FROM pair_labels"
        ).fetchall()


def labelled_keys() -> set[tuple[int, int]]:
    return {(r["article_a"], r["article_b"]) for r in labels()}


def recommend_threshold(rows_labels: list[dict], lo: float = 0.30, hi: float = 0.95,
                        step: float = 0.01) -> dict[str, Any] | None:
    """Порог, который лучше всего согласуется с разметкой владельца.

    Считаем по размеченным парам: «одна история» должна оказаться выше порога,
    «разные» — ниже. Берём порог с максимальной долей согласия, при равенстве —
    ближе к середине разрыва между классами.
    """
    if not rows_labels:
        return None

    same = [float(r["sim"]) for r in rows_labels if r["same_story"]]
    diff = [float(r["sim"]) for r in rows_labels if not r["same_story"]]
    if not same or not diff:
        return {
            "threshold": None,
            "n": len(rows_labels),
            "n_same": len(same),
            "n_diff": len(diff),
            "note": "нужны примеры обоих видов — и «одна история», и «разные»",
        }

    # считаем точность для всей сетки, затем берём СЕРЕДИНУ лучшего плато.
    # Край плато опасен: он лежит вплотную к размеченной паре, и округление
    # рекомендации до трёх знаков может перевести её на другую сторону.
    scored: list[tuple[float, float]] = []
    t = lo
    while t <= hi + 1e-9:
        tp = sum(1 for s in same if s >= t)
        tn = sum(1 for s in diff if s < t)
        scored.append((t, (tp + tn) / (len(same) + len(diff))))
        t += step

    acc = max(a for _, a in scored)
    plateau = [th for th, a in scored if a >= acc - 1e-12]
    thr = plateau[len(plateau) // 2]
    return {
        "threshold": round(thr, 3),
        "accuracy": round(acc, 3),
        "n": len(rows_labels),
        "n_same": len(same),
        "n_diff": len(diff),
        "same_min": round(min(same), 3),
        "diff_max": round(max(diff), 3),
        "overlap": min(same) <= max(diff),
    }
