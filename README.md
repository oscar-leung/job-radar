# Job Radar

**An LLM job-matching agent with a test suite that proves it works.**

Every morning Job Radar pulls new postings from public Greenhouse, Lever, Ashby, Workday and
SmartRecruiters boards plus local Davis/Sacramento sources (UC Davis, UC Davis Health, EDJOIN
school districts, Dignity Health via iCIMS), screens them with a small model, scores the survivors against my resume with a
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

## Local first, and it learns from what I do (Oct 29, 2026)

| Change | How |
|---|---|
| **Local jobs first** | Each posting gets a tier (Davis/Yolo → Sacramento area → hybrid → remote → Bay Area). Local postings are scored first within the 40-posting daily cap (capped at 60% of slots so remote QA lanes aren't starved), filed at a 10-point lower cutoff, and tagged 📍 in Notes. Order lives in `location_priority` in `config/companies.yaml`. |
| **Local sources** | UC systemwide board (UC Davis + UC Davis Health), EDJOIN (Yolo/Sacramento/Placer districts: certificated sub pools, district IT/data), SmartRecruiters (Bosch Roseville, WD, ServiceNow), iCIMS sitemap + JSON-LD (CommonSpirit/Dignity). Public endpoints only; robots.txt checked. Kaiser (robots disallows search) and CalCareers (no public API) are not scraped. |
| **Feedback loop** | Status changes on Radar rows become labels: Applied/Rejected/Ghosted = yes, Screening/Interview/Offer = strong yes, Skipped/Not Interested = no, still Not Applied after 21 days = weak no. A dozen recent decisions go into the scorer's cached system prompt as few-shot calibration (~300 tokens, cached). Location-only skips are left out, since the scorer ignores location. |
| **Monthly retune** | `python -m job_radar retune` sweeps the cutoff against Radar's own past scores vs. those labels — free, no re-scoring. It only raises the cutoff (the labels can't see below it), moves at most 10 points, needs 20+ labels, and arrives as a PR. |
| **Workday 429s** | All Workday traffic shares one limiter (3 in flight, ~5 req/s with jitter) and retries 429/5xx with exponential backoff + jitter, honoring `Retry-After`. Detail pages are fetched only for postings that will actually be scored. |
| **Weekly summary** | Sundays: a Notion page with the week's matches (local first), what was applied to, and spend vs. the monthly credit. No model calls. |

Daily cost cap unchanged: $1.50 and 40 postings per run.

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
| `evals/test_local_feedback.py` | Local tiers, backoff/limiter, new sources, feedback labels, retune, weekly page | Every push (CI) |
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

**3. Does more thinking help? (Oct 21, rubric v2.1 with all of my target lanes)**

Claude 5.5 models reason before answering; `effort` sets how hard. Same 60 labels, same cutoff:

| Model (effort) | Precision | Recall | F1 | Best cutoff → F1 | Bullet faithfulness | Scoring cost (60, batch) |
|---|---|---|---|---|---|---|
| Opus 5.5 low | 0.76 | 0.83 | 0.79 | 50 → 0.79 | 94% | $1.11 |
| Opus 5.5 medium (default) | 0.75 | 0.77 | 0.76 | 30 → 0.79 | 92% | $1.24 |
| **Opus 5.5 high** | **0.78** | **0.83** | **0.81** | 50 → 0.81 | **94%** | $0.81 |
| Opus 5.5 xhigh | 0.77 | 0.77 | 0.77 | 30 → 0.80 | 92% | $1.28 |
| Sonnet 5.5 high | 0.75 | 0.69 | 0.72 | 30 → 0.77 | 89% | $0.32 |
| Haiku 5.5 | 0.70 | 0.80 | 0.75 | 35 → 0.81 | 86% | $0.02 |

**Finding: on this task, more thinking did not measurably help.** Every Opus setting lands
between F1 0.76 and 0.81, well inside the ±0.12 bootstrap interval for 60 labels, and xhigh
was not better than low. What did separate models was **faithfulness**: Opus at any effort
invented fewer resume claims (92–94%) than Sonnet (89%) or Haiku (86%). Scoring a job is a
judgment call the model makes quickly; extra reasoning budget is better spent where the
output is long and checkable, like application kits and interview answers, which is where
this project now uses `effort=high` plus a judge-and-revise loop.

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
python -m job_radar feedback  # pull Status labels from Notion (free)
python -m job_radar retune    # suggest a cutoff from those labels (free)
python -m job_radar weekly    # print the weekly summary (add --write-notion to post it)
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
