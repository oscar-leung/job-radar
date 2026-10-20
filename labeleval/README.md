# labeleval

**Grade an LLM against human labels, as a normal pytest test.**

You built something that scores or classifies with a model: a job matcher, a ticket triager,
a content filter. Before you trust it, label a few dozen real examples yourself and check how
often the model agrees. `labeleval` makes that check a test that runs in CI, picks the cutoff
for you, tells you how much the number could move on a small sample, and, when it fails,
prints the exact cases it got wrong.

- **Precision, recall, F1, accuracy, Cohen's kappa** from yes/no labels
- **Threshold sweep:** find the score cutoff that best matches people, optionally with a precision floor
- **Bootstrap confidence intervals:** honest error bars for 30–100 hand labels
- **Model comparison:** one Markdown table across scorers, with cost
- **Misses list:** false positives and false negatives by id, the list you read to fix the prompt
- **Private labels, public items:** join compact `{id, label}` rows to a public item file
- **pytest plugin + CLI**, pure Python, no dependencies beyond pytest

## Quick start

```bash
pip install -e .
pytest examples/ -v
```

```python
def test_matcher(labeleval):
    cases = labeleval.load("labels.jsonl", "items.jsonl")       # {id, label} joined to items by id
    report = labeleval.evaluate("my-model", cases, my_scorer, threshold=50)
    labeleval.assert_meets(report, cases, min_precision=0.7, min_recall=0.65)
```

A failing bar reads like a bug report:

```
[naive-keywords] recall 0.06 < 0.80
  best cutoff would be 0 (F1 0.64)
  false negatives (15): ['t01', 't03', 't05', ...]
  false positives (2): ['t11', 't17']
```

Mark tests that call a paid model with `@pytest.mark.llm_eval` and keep them out of every-push
CI with `-m "not llm_eval"`. Add `--labeleval-results out/` to save one JSON report per scorer.

No code at all:

```bash
labeleval report labels.jsonl scores_haiku.jsonl scores_opus.jsonl --threshold 50 --sweep 30 90 5
```

## Example: ticket triage (synthetic data)

`examples/` holds 34 synthetic support tickets labeled "urgent or not" and two free scorers,
plus an optional Claude scorer.

| Scorer | n | Precision | Recall | F1 (95% CI) |
|---|---|---|---|---|
| impact-keywords | 34 | 0.88 | 0.88 | 0.88 (0.73–0.98) |
| naive-keywords (trusts the word "urgent") | 34 | 0.33 | 0.06 | 0.11 (0.00–0.32) |

The keyword scorer still misses paraphrased incidents ("conversion dropped 40% today") and
flags happy tickets that mention "all" or "every". Those four misses are exactly what an LLM
scorer is for, and the test tells you whether it actually fixes them.

## Case study: Job Radar (real data)

Built while evaluating [Job Radar](../), an LLM agent that scores job postings against a
resume. 60 real postings, hand-labeled by the candidate (35 "would apply", 25 "skip"):

| Version | Cutoff | Precision | Recall | F1 (95% CI) | Cohen's κ |
|---|---|---|---|---|---|
| Rubric v1 | 70 | 1.00 | 0.03 | 0.06 (0.00–0.16) | 0.02 |
| Rubric v2 | 70 | 0.89 | 0.23 | 0.36 (0.17–0.53) | 0.16 |
| Rubric v2 | **50** (from the sweep) | 0.75 | 0.69 | **0.72 (0.58–0.82)** | 0.36 |

v1 looked "precise" and was useless: it agreed with 1 of 35 picks. The sweep and the misses
list are what found and fixed it. The wide interval on 60 labels is the honest part: the
true F1 could be anywhere from about 0.6 to 0.8.

## API

| Function | Does |
|---|---|
| `load_cases(labels, items=None)` | Load JSONL labels (optionally joined to items by id) → `Case` list |
| `evaluate(name, cases, scorer \| scores=, threshold=)` | Score and grade → `Report` |
| `Report.summary()` / `.misses(cases)` | Numbers dict / wrong ids |
| `compare(reports)` | Markdown comparison table |
| `sweep`, `best_threshold`, `bootstrap_ci`, `cohen_kappa`, `confusion` | The metrics, usable alone |
| `faithfulness(verdicts)` | Share of generated claims a judge found supported |

## License

MIT
