# Job Radar — guide for Claude Code sessions

Run build sessions headless on the API credit (not the Max plan):

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # key from the "job-radar" Console workspace
claude -p "<task>" --model claude-opus-5-5
```

## What this repo is
A daily agent that pulls postings from public Greenhouse/Lever/Ashby/Workday APIs, filters
with Haiku, scores fit with Opus against the user's resume, and writes matches into a Notion
tracker as `Status = Not Applied`. Who it searches for lives in `config/profile.yaml`; the
same code serves the author and Pro Kit buyers (MIT core, paid template repo). A pytest
eval suite measures scoring accuracy against hand labels and checks bullets for fabrication.

## Layout
- `src/job_radar/profile.py` — config/profile.yaml: candidate, places, lanes, prompt placeholders
- `src/job_radar/ingest.py` — ATS fetchers, HTML cleanup, keyword prefilter
- `src/job_radar/score.py` — forced-tool structured output, cached system prompt
- `src/job_radar/notion_out.py` — tracker column mapping, dedupe on External ID, `init-notion` bootstrap
- `src/job_radar/cost.py` — per-call cost + hard per-run budget
- `src/job_radar/sweep.py` — Batch API cover-note drafts for the backlog
- `evals/` — offline unit tests, live eval, model comparison, labeling helper

## Rules (do not break)
1. Never submit, send, or apply to anything. Output goes to Notion as drafts only.
2. Generated resume text may use only facts in `data/resume.md`. Never invent experience,
   metrics, or years of experience.
3. Never commit `.env`, `data/resume.md`, `evals/labels.jsonl`, or API keys.
4. Every model call must go through `CostMeter`; keep `RADAR_MAX_RUN_USD` enforced.
5. Report only real eval numbers in the README — no placeholder results presented as real.
6. `pytest -q` (offline) must pass before every commit.
7. Nothing personal in code or prompts: names, places and lanes come from `config/profile.yaml`
   via `{{PLACEHOLDERS}}`. `evals/test_profile.py` fails on a leftover placeholder.

## Good next tasks
- Add a `--since` filter using posting dates where the ATS exposes them
- Add more boards to `config/companies.yaml` and verify with `check-boards`
- Calibrate `RADAR_FIT_THRESHOLD` from `evals/results/` (pick the best F1)
- Static HTML report published to GitHub Pages from `reports/`
