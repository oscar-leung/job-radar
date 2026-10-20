import json

import pytest

from labeleval import (best_threshold, bootstrap_ci, cohen_kappa, compare, confusion, evaluate,
                       faithfulness, load_cases, sweep)

pytest_plugins = ["pytester"]


def test_confusion_and_rates():
    c = confusion([True, True, False, False, True], [True, False, True, False, True])
    assert (c.tp, c.fp, c.fn, c.tn) == (2, 1, 1, 1)
    assert c.precision == pytest.approx(2 / 3) and c.recall == pytest.approx(2 / 3)
    assert c.accuracy == pytest.approx(3 / 5)


def test_confusion_length_mismatch():
    with pytest.raises(ValueError):
        confusion([True], [True, False])


def test_empty_denominators_are_zero_not_errors():
    c = confusion([False, False], [False, False])
    assert c.precision == 0.0 and c.recall == 0.0 and c.f1 == 0.0


def test_sweep_and_best_threshold():
    gold = [True, True, True, False, False]
    scores = [90, 70, 40, 60, 10]
    pts = {p.threshold: p for p in sweep(gold, scores)}
    assert pts[70].precision == 1.0 and pts[70].recall == pytest.approx(2 / 3)
    best = best_threshold(gold, scores)
    assert best.threshold == 40 and best.recall == 1.0
    assert best_threshold(gold, scores, min_precision=1.0).threshold == 70


def test_best_threshold_unreachable_floor():
    with pytest.raises(ValueError):
        best_threshold([True, False], [10, 90], min_precision=1.0)


def test_bootstrap_ci_brackets_point_estimate_and_is_reproducible():
    gold = [True] * 20 + [False] * 20
    pred = [True] * 15 + [False] * 5 + [True] * 4 + [False] * 16
    lo, hi = bootstrap_ci(gold, pred, "f1", n_boot=500, seed=1)
    point = confusion(gold, pred).f1
    assert lo <= point <= hi and hi - lo > 0.05
    assert (lo, hi) == bootstrap_ci(gold, pred, "f1", n_boot=500, seed=1)


def test_cohen_kappa():
    assert cohen_kappa([True, False, True, False], [True, False, True, False]) == pytest.approx(1.0)
    assert cohen_kappa([True, True, False, False], [True, False, True, False]) == pytest.approx(0.0)


def test_load_cases_joins_compact_labels(tmp_path):
    items = tmp_path / "items.jsonl"
    items.write_text(json.dumps({"id": "a", "text": "outage"}) + "\n" + json.dumps({"id": "b", "text": "hi"}))
    labels = tmp_path / "labels.jsonl"
    labels.write_text('{"id": "a", "label": "good"}\n{"id": "b", "label": "bad"}\n{"id": "zz", "label": "good"}\n')
    cases = load_cases(labels, items)
    assert [(c.id, c.label, c.item["text"]) for c in cases] == [("a", True, "outage"), ("b", False, "hi")]


def test_evaluate_reports_misses_and_compare_table():
    cases = load_cases_from([("a", True), ("b", True), ("c", False), ("d", False)])
    good = evaluate("good", cases, scores={"a": 9, "b": 8, "c": 1, "d": 2}, threshold=5, cost_usd=0.5)
    bad = evaluate("bad", cases, scores={"a": 9, "b": 1, "c": 8, "d": 2}, threshold=5)
    assert good.confusion.f1 == 1.0
    assert bad.misses(cases) == {"false_positives": ["c"], "false_negatives": ["b"]}
    table = compare([bad, good])
    assert table.index("| good") < table.index("| bad") and "$0.50" in table


def test_evaluate_requires_all_scores():
    with pytest.raises(KeyError):
        evaluate("x", load_cases_from([("a", True)]), scores={})


def test_faithfulness():
    assert faithfulness([True, True, False, True]) == 0.75 and faithfulness([]) == 0.0


def test_plugin_fixture_and_failure_message(pytester):
    pytester.makepyfile("""
        from labeleval import Case
        def test_bar(labeleval):
            cases = [Case("a", True), Case("b", True), Case("c", False)]
            r = labeleval.evaluate("m", cases, scores={"a": 9, "b": 1, "c": 2}, threshold=5)
            labeleval.assert_meets(r, cases, min_recall=0.9)
    """)
    res = pytester.runpytest()  # plugin loads via its pytest11 entry point
    res.assert_outcomes(failed=1)
    out = res.stdout.str()  # fnmatch would read the brackets in "['b']" as a character class
    assert "recall 0.50 < 0.90" in out and "false negatives (1): ['b']" in out


def load_cases_from(pairs):
    from labeleval import Case
    return [Case(i, lab) for i, lab in pairs]
