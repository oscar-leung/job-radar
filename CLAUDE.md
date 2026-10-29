# Job Radar — guide for Claude Code sessions

Run build sessions headless on the API credit (not the Max plan):

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # key from the "job-radar" Console workspace
claude -p "<task>" --model claude-opus-5-5
```

## What this repo is
A daily agent that pulls QA/SDET and AI-eval postings from public Greenhouse/Lever/Ashby
APIs, filters with Haiku, scores fit with Sonnet against the owner's resume, and writes
matches into the Notion "Master Job Tracker — Live" as `Status = Not Applied`. A pytest
eval suite measures scoring accuracy against hand labels and checks bullets for fabrication.

## Layout
- `src/job_radar/ingest.py` — ATS + local-source fetchers (UC, EDJOIN, SmartRecruiters, iCIMS), detail hydration, prefilters
- `src/job_radar/http.py` — per-host limiters + retry with exponential backoff/jitter (Workday 429s)
- `src/job_radar/locality.py` — local-first tiers, queue order, local cutoff bonus
- `src/job_radar/feedback.py` — Status → labels, few-shot block, monthly retune sweep
- `src/job_radar/weekly.py` — weekly Notion summary (matches, applied, spend)
- `src/job_radar/score.py` — forced-tool structured output, cached system prompt
- `src/job_radar/notion_out.py` — exact tracker column mapping + dedupe on External ID
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
7. Only public job-board endpoints; check robots.txt before adding a source. No logins, no scraping
   behind auth. All board traffic goes through `http.request` with a limiter.

## Good next tasks
- Add a `--since` filter using posting dates where the ATS exposes them
- Add more boards to `config/companies.yaml` and verify with `check-boards`
- Calibrate `RADAR_FIT_THRESHOLD` from `evals/results/` (pick the best F1)
- Static HTML report published to GitHub Pages from `reports/`
