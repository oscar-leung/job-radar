"""pytest plugin: turn "does the model agree with people?" into a normal test.

    def test_matcher(labeleval):
        cases = labeleval.load("labels.jsonl", items="items.jsonl")
        report = labeleval.evaluate("sonnet", cases, my_scorer, threshold=50)
        labeleval.assert_meets(report, cases, min_precision=0.7, min_recall=0.65)

Tests that call a paid model should carry `@pytest.mark.llm_eval` so CI can deselect them
(`-m "not llm_eval"`) and run them on demand. A failed bar prints the misses to read next.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from . import core


class LabelEval:
    load = staticmethod(core.load_cases)
    evaluate = staticmethod(core.evaluate)
    compare = staticmethod(core.compare)

    def __init__(self, results_dir: Path | None):
        self.results_dir = results_dir
        self.reports: list[core.Report] = []

    def assert_meets(self, report: core.Report, cases, *, min_precision: float = 0.0,
                     min_recall: float = 0.0, min_f1: float = 0.0) -> None:
        self.reports.append(report)
        if self.results_dir:
            self.results_dir.mkdir(parents=True, exist_ok=True)
            (self.results_dir / f"{report.name}.json").write_text(json.dumps(
                {**report.summary(), **report.misses(cases),
                 "curve": [p.__dict__ for p in report.curve]}, indent=2))
        c = report.confusion
        problems = [f"{name} {got:.2f} < {want:.2f}" for name, got, want in (
            ("precision", c.precision, min_precision), ("recall", c.recall, min_recall),
            ("f1", c.f1, min_f1)) if got < want]
        if problems:
            m = report.misses(cases)
            pytest.fail(
                f"[{report.name}] " + "; ".join(problems)
                + f"\n  best cutoff would be {report.best.threshold:g} (F1 {report.best.f1:.2f})"
                + f"\n  false negatives ({len(m['false_negatives'])}): {m['false_negatives'][:10]}"
                + f"\n  false positives ({len(m['false_positives'])}): {m['false_positives'][:10]}",
                pytrace=False)


def pytest_addoption(parser):
    parser.addoption("--labeleval-results", default=None,
                     help="directory to write one JSON report per evaluated scorer")


def pytest_configure(config):
    config.addinivalue_line("markers", "llm_eval: calls a paid model; deselect with -m 'not llm_eval'")


@pytest.fixture
def labeleval(request) -> LabelEval:
    d = request.config.getoption("--labeleval-results")
    return LabelEval(Path(d) if d else None)
