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
ATS APIs ──► title regex (free) ──► Haiku 5.5 relevance ──► Sonnet 5.5 fit score ──► Notion
                                     (forced tool call)      (structured JSON,          (dedupe on
                                                              cached resume prefix)      External ID)
```

| Stage | Why |
|---|---|
| Regex on titles | Drops most postings before any model call, at zero cost |
| Haiku relevance filter | Cheap yes/no, generous by design |
| Sonnet fit scoring | 0–100 score, fit band, lane, location, gaps, 3 bullets — schema-enforced via forced tool use |
| Prompt caching | Resume + rubric is a shared prefix; cache reads cost a fraction of normal input |
| Cost meter | Every call is priced from `usage`; a hard per-run budget stops the run |

## Evaluation

| Test | What it checks | Runs |
|---|---|---|
| `evals/test_unit.py` | Parsing, prefilter, cost math, Notion mapping, metrics | Every push (CI) |
| `evals/test_live_eval.py` | Precision/recall vs. hand labels, with a regression bar | Manual (spends credits) |
| `evals/compare_models.py` | 4 models × labeled set via Batch API, plus bullet-faithfulness judge | Manual |

### Results

_Results will appear here after the first eval run. Only measured numbers are published._

<!-- paste evals/results/comparison.md here -->

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
