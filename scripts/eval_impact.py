#!/usr/bin/env python3
"""Оценка классификатора impact на размеченной выборке.

Вход — JSONL, по строке на кластер:
    {"id": 1, "headline": "...", "label": "keep|drop", "impact": "consequential"}

`label` ставится вручную, `impact` — вывод классификатора
(scripts/impact_dataset.py classify).

Использование:
    python scripts/eval_impact.py --file labeled.jsonl
    python scripts/eval_impact.py --file labeled.jsonl --holdout   # отложенная треть
    python scripts/eval_impact.py --file labeled.jsonl --train     # калибровочные две трети

Печатает матрицу ошибок и список расхождений, чтобы было видно,
на каких формулировках промпт расходится с разметкой.
"""
import argparse
import json
from collections import Counter
from pathlib import Path

# что считается публикацией отдельным постом
POSTED = {"consequential"}


def load(path, holdout=None):
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    # строки без ручной разметки в счёт не идут: пустой label — это не «drop»
    rows = [r for r in rows if r.get("label") in ("keep", "drop")]
    if holdout is None:
        return rows
    # детерминированное деление: каждый третий — отложенный
    return [r for i, r in enumerate(rows) if (i % 3 == 2) == holdout]


def evaluate(rows):
    tp = fp = tn = fn = 0
    misses, junk = [], []

    for r in rows:
        want = r.get("label") == "keep"
        got = r.get("impact") in POSTED
        if want and got:
            tp += 1
        elif want and not got:
            fn += 1
            misses.append(r)
        elif not want and got:
            fp += 1
            junk.append(r)
        else:
            tn += 1

    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    return {"tp": tp, "fp": fp, "tn": tn, "fn": fn,
            "precision": prec, "recall": rec,
            "misses": misses, "junk": junk}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--holdout", action="store_true",
                    help="считать только по отложенной трети")
    ap.add_argument("--train", action="store_true",
                    help="считать только по калибровочным двум третям")
    a = ap.parse_args()

    holdout = True if a.holdout else (False if a.train else None)
    rows = load(a.file, holdout)
    res = evaluate(rows)

    print(f"кластеров: {len(rows)}")
    print(f"  опубликовано верно : {res['tp']}")
    print(f"  мусор пропущен     : {res['fp']}   <- дорогая ошибка")
    print(f"  новость потеряна   : {res['fn']}")
    print(f"  мусор отсечён      : {res['tn']}")
    print(f"\nточность (precision): {res['precision']:.2f}")
    print(f"полнота  (recall)   : {res['recall']:.2f}")

    print("\nраспределение impact:")
    for k, v in Counter(r.get("impact") for r in rows).most_common():
        print(f"  {k}: {v}")

    if res["junk"]:
        print("\nмусор, прошедший в посты:")
        for r in res["junk"][:15]:
            print(f"  [{r.get('impact')}] {r.get('headline','')[:90]}")

    if res["misses"]:
        print("\nновости, потерянные классификатором:")
        for r in res["misses"][:15]:
            print(f"  [{r.get('impact')}] {r.get('headline','')[:90]}")


if __name__ == "__main__":
    main()
