"""Classification metrics for the eval suite."""
from __future__ import annotations


def confusion(gold: list[bool], pred: list[bool]) -> dict[str, int]:
    c = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    for g, p in zip(gold, pred):
        c["tp" if g and p else "fp" if p else "fn" if g else "tn"] += 1
    return c


def precision_recall(c: dict[str, int]) -> tuple[float, float, float]:
    p = c["tp"] / (c["tp"] + c["fp"]) if c["tp"] + c["fp"] else 0.0
    r = c["tp"] / (c["tp"] + c["fn"]) if c["tp"] + c["fn"] else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f1
