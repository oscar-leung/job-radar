# Job Radar

**An LLM job-matching agent with a test suite that proves it works.**

Every morning Job Radar pulls new postings from public Greenhouse, Lever, and Ashby job
boards, screens them with a small model, scores the survivors against my resume with a
larger one, and files strong matches into my Notion job tracker with three tailored resume
bullets each. Nothing is ever submitted automatically.

The interesting part is the evaluation: a pytest suite measures how often the scorer agrees
with my own hand labels, compares four Claude models on accuracy and cost via the Batch API,
and uses a judge model to catch any bullet that claims experience I don't have.

![CI](https://github.com/oscar-leung/job-radar/actions/workflows/ci.yml/badge.svg)

## How it works

```
ATS APIs ──► title regex (free) ──► Haiku 5.5 relevance ──► Opus 5.5 fit score ───► Notion
                                     (JSON schema output)    (JSON schema output,       (dedupe on
                                                              cached resume prefix)      External ID)
```

| Stage | Why |
|---|---|
| Regex on titles | Drops most postings before any model call, at zero cost |
| Haiku relevance filter | Cheap yes/no, generous by design |
| Opus fit scoring | 0–100 score, fit band, lane, location, gaps, 3 bullets — schema-enforced via structured outputs |
| Location gate | Only files roles in my work area (remote US, Davis/Sacramento, Bay Area) |
| Prompt caching | Resume + rubric is a shared prefix; cache reads cost a fraction of normal input |
| Cost meter | Every call is priced from `usage`; a hard per-run budget stops the run |

## First live run (Oct 19, 2026)

| Stage | Count |
|---|---|
| Postings pulled from 40 company boards | 10,632 |
| Passed the free title filter | 249 |
| Passed the Haiku relevance screen and fully scored | 145 |
| Filed to the tracker (fit ≥ 60) | 17 |
| **Total API cost** | **$1.63** |

The free regex filter removed about 98% of postings before any model call; the cheap
Haiku screen removed another 42% of what was left.

### What broke on the way to production, and the fix

| Failure | Cause | Fix |
|---|---|---|
| Notion `429 Too Many Requests` | Dedupe made one query per posting (~250) | Load existing IDs in one paged pass; retry with `Retry-After` |
| `400 tool_choice … not supported` | Claude 5.5 models reject forced tool calls | Moved to structured outputs (`output_config.format`, JSON schema) |
| `JSONDecodeError: Unterminated string` | A reply hit `max_tokens` mid-JSON | Detect `stop_reason == "max_tokens"`, retry once with double room; one bad posting is skipped, not fatal |
| `401 invalid x-api-key` | Wrong key pasted into the repo secret | Preflight step names a missing or malformed secret before any spend |

Each fix ships with a unit test (24 offline tests).

## Evaluation

| Test | What it checks | Runs |
|---|---|---|
| `evals/test_unit.py` | Parsing, prefilter, cost math, Notion mapping, metrics | Every push (CI) |
| `evals/test_live_eval.py` | Precision/recall vs. hand labels, with a regression bar | Manual (spends credits) |
| `evals/compare_models.py` | 4 models × labeled set via Batch API, plus bullet-faithfulness judge | Manual |

### Results (60 hand-labeled postings, Oct 19–20, 2026)

**1. The eval caught a bad rubric.** I labeled 60 real postings "would apply" (35) or "skip" (25)
and graded the scorer against them.

| Rubric | Cutoff | Precision | Recall | F1 |
|---|---|---|---|---|
| v1 ("score honestly", penalize level and gaps) | 70 | 1.00 | **0.03** | 0.06 |
| v2 (score the work, not title seniority or location) | 70 | 0.89 | 0.23 | 0.36 |
| v2 | **50** (picked by threshold sweep) | **0.75** | **0.69** | **0.72** |

v1 agreed with only 1 of my 35 "apply" picks. It was marking down Senior/Staff QA and eval roles
I'd actually go for, and letting location drag scores down even though location is filtered
separately. Caveat: v2 and the cutoff were tuned on the same 60 postings, so expect real-world
numbers to be somewhat lower.

**2. Four models, same labels, same cutoff (Batch API):**

| Model | Precision | Recall | F1 | Bullet faithfulness | Scoring cost (60 postings) |
|---|---|---|---|---|---|
| Haiku 5.5 | 0.72 | 0.83 | 0.77 | 87% | $0.02 |
| Sonnet 5.5 | 0.75 | 0.69 | 0.72 | 89% | $0.33 |
| **Opus 5.5** | 0.76 | 0.83 | **0.79** | **90%** | $0.87 |
| Fable 5.1 | **0.83** | 0.71 | 0.77 | 79% | $2.83 |

- **Opus** is the daily scorer: best F1 and the most faithful bullets, at about 1.5¢ a posting (batch price; about 3¢ in the daily run).
- **Haiku** nearly matches it at 1/36th the cost, the right fallback if volume grows.
- **The biggest model isn't the best here:** Fable had the highest precision but the least faithful
  bullets and cost 3× Opus.
- **Faithfulness** = share of tailored resume bullets a judge model found fully supported by the
  resume. Even the best model invents something in about 1 bullet in 10, which is why every
  bullet lands in Notion marked "verify before use".

## labeleval: the eval toolkit, reusable

The grading code grew into its own package, [`labeleval/`](labeleval/): a pytest plugin and
CLI for grading any LLM (or any scorer) against human labels, with threshold sweeps,
bootstrap confidence intervals, model comparison tables and a misses list. It has its own
tests, a synthetic ticket-triage example, and the Job Radar numbers above with error bars.

## Run it

```bash
pip install -e ".[dev]"
cp .env.example .env          # add your keys
cp your-resume.md data/resume.md
python -m job_radar check-boards
python -m job_radar run --dry-run --limit 10
pytest -q                     # offline tests
python evals/label_helper.py --n 50
pytest -m live evals/test_live_eval.py
python evals/compare_models.py
```

See [SETUP.md](SETUP.md) for Console, Notion, and GitHub Actions setup.

## Design choices

- **Human in the loop.** Matches land as `Not Applied`. A person decides what to apply to.
- **No fabrication.** The rubric forbids inventing experience, and the judge measures it.
- **Cost visible by default.** Every run appends to `costs.csv`; reports include cost per run.
- **Private data stays private.** Resume and labels live in secrets, never in the repo.
