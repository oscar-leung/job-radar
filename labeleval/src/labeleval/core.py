"""Run one or more scorers over a labeled set and report how well they agree with humans."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .metrics import Confusion, SweepPoint, at_threshold, best_threshold, bootstrap_ci, sweep

Scorer = Callable[[dict], float]
"""Anything that maps one item (a dict) to a numeric score. Higher = more likely positive."""


@dataclass
class Case:
    id: str
    label: bool
    item: dict = field(default_factory=dict)


def load_cases(labels_path: str | Path, items_path: str | Path | None = None,
               id_key: str = "id", label_key: str = "label",
               positive: Iterable[Any] = (True, "good", "yes", 1)) -> list[Case]:
    """Load JSONL labels. If `items_path` is given, labels can be compact
    ({id, label}) and are joined to full items by id — handy for keeping human labels
    private while the items they grade stay public."""
    pos = set(positive)
    items = {}
    if items_path:
        for line in Path(items_path).read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                items[str(row[id_key])] = row
    cases = []
    for line in Path(labels_path).read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        cid = str(row[id_key])
        item = {**items.get(cid, {}), **row}
        if items_path and cid not in items:
            continue
        cases.append(Case(cid, row[label_key] in pos, item))
    return cases


@dataclass
class Report:
    name: str
    threshold: float
    confusion: Confusion
    best: SweepPoint
    curve: list[SweepPoint]
    f1_ci: tuple[float, float]
    scores: dict[str, float]
    cost_usd: float | None = None

    def summary(self) -> dict:
        return {"name": self.name, "n": self.confusion.n, "threshold": self.threshold,
                **{k: round(v, 4) if isinstance(v, float) else v
                   for k, v in self.confusion.as_dict().items()},
                "f1_ci95": [round(x, 3) for x in self.f1_ci],
                "best_threshold": self.best.threshold, "best_f1": round(self.best.f1, 4),
                "cost_usd": self.cost_usd}

    def misses(self, cases: Sequence[Case]) -> dict[str, list[str]]:
        """Ids the scorer got wrong — the list you read to improve the prompt."""
        fp, fn = [], []
        for c in cases:
            pred = self.scores[c.id] >= self.threshold
            if pred and not c.label:
                fp.append(c.id)
            elif c.label and not pred:
                fn.append(c.id)
        return {"false_positives": fp, "false_negatives": fn}


def evaluate(name: str, cases: Sequence[Case], scorer: Scorer | None = None, *,
             scores: dict[str, float] | None = None, threshold: float = 0.5,
             thresholds: Sequence[float] | None = None, cost_usd: float | None = None) -> Report:
    """Grade one scorer. Pass `scorer` to compute scores, or precomputed `scores` by id."""
    if scores is None:
        if scorer is None:
            raise ValueError("pass a scorer or precomputed scores")
        scores = {c.id: float(scorer(c.item)) for c in cases}
    missing = [c.id for c in cases if c.id not in scores]
    if missing:
        raise KeyError(f"no score for {len(missing)} cases, e.g. {missing[:3]}")
    gold = [c.label for c in cases]
    s = [scores[c.id] for c in cases]
    conf = at_threshold(gold, s, threshold)
    return Report(name=name, threshold=threshold, confusion=conf,
                  best=best_threshold(gold, s, thresholds), curve=sweep(gold, s, thresholds),
                  f1_ci=bootstrap_ci(gold, [x >= threshold for x in s], "f1"),
                  scores=dict(scores), cost_usd=cost_usd)


def compare(reports: Sequence[Report]) -> str:
    """Markdown table comparing scorers on the same labels."""
    head = ("| Scorer | n | Precision | Recall | F1 (95% CI) | Best cutoff → F1 | Cost |\n"
            "|---|---|---|---|---|---|---|")
    rows = []
    for r in sorted(reports, key=lambda r: -r.confusion.f1):
        c = r.confusion
        cost = f"${r.cost_usd:.2f}" if r.cost_usd is not None else "—"
        rows.append(f"| {r.name} | {c.n} | {c.precision:.2f} | {c.recall:.2f} | "
                    f"{c.f1:.2f} ({r.f1_ci[0]:.2f}–{r.f1_ci[1]:.2f}) | "
                    f"{r.best.threshold:g} → {r.best.f1:.2f} | {cost} |")
    return "\n".join([head, *rows])


def faithfulness(verdicts: Iterable[bool]) -> float:
    """Share of generated claims a judge found supported by the source (1.0 = nothing invented)."""
    v = list(verdicts)
    return sum(v) / len(v) if v else 0.0
