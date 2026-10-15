# Setup (≈20 minutes)

## 1. Claude Console (platform.claude.com)
1. **Settings → Organization settings → Workspaces → Create workspace** named `job-radar`.
2. In that workspace, set a **spend limit** (e.g. $190) so one project can't drain the org.
3. **API keys → Create key** in the `job-radar` workspace. Copy it once; store it in your
   password manager.
4. **Billing → Spend limits → Add notification** at $100 and $175.
5. Leave **auto-reload off** and no payment method on file. When the $200 runs out,
   requests just stop.

Credits: $200, expire **Oct 24, 2026**. Next $200 arrives with the next Max renewal.

## 2. Notion
1. notion.so/profile/integrations → **New integration** (internal) → copy the token.
2. Open **🎯 Master Job Tracker — Live** → `•••` → **Connections** → add the integration.
3. Database ID is already set in `.env.example` (`d2c0fa46a231491cbe754cbf5abf272e`).

Radar rows are easy to filter: `Notes` starts with `RADAR:` and `Status = Not Applied`.

## 3. Local
```bash
git clone https://github.com/oscar-leung/job-radar && cd job-radar
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # paste ANTHROPIC_API_KEY and NOTION_TOKEN
# paste your resume text into data/resume.md (gitignored)
pytest -q
python -m job_radar check-boards
python -m job_radar run --dry-run --limit 10
```

## 4. GitHub Actions (runs in the cloud, MacBook can sleep)
Repo → Settings → Secrets and variables → Actions → add:
- `ANTHROPIC_API_KEY`
- `NOTION_TOKEN`
- `RESUME_MD` — full resume text
- `LABELS_JSONL` — contents of `evals/labels.jsonl` (for the live eval workflow)

`Radar daily` then runs every morning. Trigger it once by hand (Actions → Radar daily →
Run workflow → dry run) to confirm.

## 5. Build with Claude Code on the API credit (saves your Max weekly limit)
```bash
export ANTHROPIC_API_KEY=sk-ant-...      # the job-radar key
claude -p "Read CLAUDE.md. Add 8 more QA/AI-eval company boards to config/companies.yaml and verify them with check-boards." --model claude-opus-5-5
```
Signed in with your Max login instead, Claude Code uses your plan limits, not the credit.

## Alternative to step 4: Managed Agents
Console → **Managed Agents → Quickstart** can host a scheduled agent instead of GitHub
Actions. It adds $0.08 per running session-hour on top of tokens and has no batch discount,
so Actions is cheaper for this job. Worth trying once if you want to list it as a skill.
