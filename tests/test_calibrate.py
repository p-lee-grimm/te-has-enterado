"""Рекомендация порога по ручной разметке.

Логика решения, а не кластеризация: её тестировать можно и нужно.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quepasa.calibrate import recommend_threshold  # noqa: E402


def lab(sim: float, same: bool) -> dict:
    return {"article_a": 1, "article_b": 2, "same_story": same, "sim": sim}


class TestRecommendThreshold:
    def test_no_labels_returns_none(self):
        assert recommend_threshold([]) is None

    def test_one_sided_labels_ask_for_the_other_kind(self):
        res = recommend_threshold([lab(0.9, True), lab(0.8, True)])
        assert res["threshold"] is None
        assert "обоих" in res["note"]

    def test_cleanly_separated_classes(self):
        """Разрыв между классами — порог должен встать внутрь него."""
        rows = [lab(0.90, True), lab(0.88, True), lab(0.40, False), lab(0.35, False)]
        res = recommend_threshold(rows)
        assert 0.40 < res["threshold"] <= 0.88
        assert res["accuracy"] == 1.0
        assert res["overlap"] is False

    def test_overlap_is_reported(self):
        """Пересечение классов — идеального порога нет, и об этом надо сказать."""
        rows = [lab(0.60, True), lab(0.42, True), lab(0.47, False), lab(0.30, False)]
        res = recommend_threshold(rows)
        assert res["overlap"] is True
        assert res["accuracy"] < 1.0

    def test_counts_reported(self):
        rows = [lab(0.9, True), lab(0.8, True), lab(0.2, False)]
        res = recommend_threshold(rows)
        assert res["n"] == 3 and res["n_same"] == 2 and res["n_diff"] == 1

    def test_threshold_admits_all_same_pairs_when_separable(self):
        rows = [lab(0.71, True), lab(0.70, True), lab(0.50, False)]
        res = recommend_threshold(rows)
        # порог не должен отсекать размеченное как «одна история»
        assert res["threshold"] <= 0.70
        assert res["accuracy"] == 1.0
