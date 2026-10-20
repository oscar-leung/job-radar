"""Metrics for grading a scoring model against human yes/no labels.

Everything here is pure Python with no dependencies, so it runs in CI for free.
"""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass
from typing import Sequence


@dataclass(frozen=True)
class Confusion:
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def n(self) -> int:
        return self.tp + self.fp + self.fn + self.tn

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    @property
    def accuracy(self) -> float:
        return (self.tp + self.tn) / self.n if self.n else 0.0

    def as_dict(self) -> dict:
        return {**asdict(self), "precision": self.precision, "recall": self.recall,
                "f1": self.f1, "accuracy": self.accuracy}


def confusion(gold: Sequence[bool], pred: Sequence[bool]) -> Confusion:
    if len(gold) != len(pred):
        raise ValueError(f"gold has {len(gold)} labels but pred has {len(pred)}")
    tp = fp = fn = tn = 0
    for g, p in zip(gold, pred):
        if g and p:
            tp += 1
        elif p:
            fp += 1
        elif g:
            fn += 1
        else:
            tn += 1
    return Confusion(tp, fp, fn, tn)


def at_threshold(gold: Sequence[bool], scores: Sequence[float], threshold: float) -> Confusion:
    """Predict positive when score >= threshold."""
    return confusion(gold, [s >= threshold for s in scores])


@dataclass(frozen=True)
class SweepPoint:
    threshold: float
    precision: float
    recall: float
    f1: float


def sweep(gold: Sequence[bool], scores: Sequence[float],
          thresholds: Sequence[float] | None = None) -> list[SweepPoint]:
    """Precision/recall/F1 at each threshold. Defaults to every distinct score."""
    ts = sorted(set(thresholds if thresholds is not None else scores))
    out = []
    for t in ts:
        c = at_threshold(gold, scores, t)
        out.append(SweepPoint(t, c.precision, c.recall, c.f1))
    return out


def best_threshold(gold: Sequence[bool], scores: Sequence[float],
                   thresholds: Sequence[float] | None = None,
                   min_precision: float = 0.0) -> SweepPoint:
    """Highest-F1 threshold, optionally among those meeting a precision floor.
    Ties go to the higher threshold (fewer false positives)."""
    pts = [p for p in sweep(gold, scores, thresholds) if p.precision >= min_precision]
    if not pts:
        raise ValueError(f"no threshold reaches precision {min_precision}")
    return max(pts, key=lambda p: (round(p.f1, 9), p.threshold))


def bootstrap_ci(gold: Sequence[bool], pred: Sequence[bool], metric: str = "f1",
                 n_boot: int = 2000, alpha: float = 0.05, seed: int = 0) -> tuple[float, float]:
    """Percentile bootstrap confidence interval for precision/recall/f1/accuracy.

    Small labeled sets are the norm for hand-graded LLM evals; the interval shows how
    much a number could move with a different sample of the same size.
    """
    if metric not in {"precision", "recall", "f1", "accuracy"}:
        raise ValueError(f"unknown metric {metric!r}")
    rng = random.Random(seed)
    idx = list(range(len(gold)))
    vals = []
    for _ in range(n_boot):
        s = [rng.choice(idx) for _ in idx]
        vals.append(getattr(confusion([gold[i] for i in s], [pred[i] for i in s]), metric))
    vals.sort()
    lo = vals[int((alpha / 2) * n_boot)]
    hi = vals[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]
    return lo, hi


def cohen_kappa(a: Sequence[bool], b: Sequence[bool]) -> float:
    """Agreement between two raters beyond chance (e.g. model vs human, or two humans)."""
    c = confusion(a, b)
    n = c.n
    if not n:
        return 0.0
    po = (c.tp + c.tn) / n
    pa_yes = (c.tp + c.fn) / n
    pb_yes = (c.tp + c.fp) / n
    pe = pa_yes * pb_yes + (1 - pa_yes) * (1 - pb_yes)
    return (po - pe) / (1 - pe) if pe != 1 else 1.0
