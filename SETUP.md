# Setup (about 20 minutes, 10 with the Pro Kit)

Three accounts, four secrets, one YAML file. Nothing here costs money except the API calls
your own runs make, and each run has a hard dollar cap.

## 1. Claude Console (platform.claude.com)
1. **Settings → Organization settings → Workspaces → Create workspace** named `job-radar`.
2. In that workspace, set a **spend limit** (for example $20) so this project can never drain
   your account. The Lite preset uses about $1.50 a month; Best about $45.
3. **API keys → Create key** in that workspace. Copy it once; store it in your password manager.
4. **Billing → Spend limits → Add notification** at half the limit.

## 2. Notion
1. notion.so/profile/integrations → **New integration** (internal) → copy the token (`ntn_...`).
2. Make or pick a page the tracker will live under (for example "Job Search"). On that page:
   `•••` → **Connections** → add the integration.
3. Create the tracker database (prints `NOTION_DATABASE_ID`). In the browser: add the
   `NOTION_TOKEN` secret (step 5), then Actions → **Init Notion** → Run workflow → paste the
   page URL. Or locally:
   ```bash
   NOTION_TOKEN=ntn_... python -m job_radar init-notion --parent "<that page's URL>"
   ```
   Already have a tracker? `python -m job_radar init-notion --check` lists any column the
   radar needs that your database is missing or has with the wrong type.

Radar rows are easy to filter: `Notes` starts with `RADAR:` and `Status = Not Applied`.

## 3. Describe yourself: `config/profile.yaml`
- `home`, `work_country`, `in_area`: where you live and the places you would commute to.
- `lanes`, `resume_versions`: the categories the scorer sorts matches into.
- `lane_guidance`: a few bullets on how you decide. The scorer is calibrated against this.
- `relevant_work`: which kinds of job deserve a model call at all.
- Optional `title_include` / `title_exclude` / `location_include` override the free regex
  filters in `config/companies.yaml`, so a lane pack is one file.

Your resume never goes in the repo: `data/resume.md` is gitignored and CI reads the
`RESUME_MD` secret.

## 4. Local run
```bash
git clone <your fork> && cd job-radar
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # paste ANTHROPIC_API_KEY, NOTION_TOKEN, NOTION_DATABASE_ID
cp /path/to/resume.md data/resume.md
pytest -q
python -m job_radar check-boards
python -m job_radar run --dry-run --limit 10
```

## 5. GitHub Actions (runs in the cloud; your laptop can sleep)
Repo → Settings → Secrets and variables → Actions → **Secrets**:
- `ANTHROPIC_API_KEY`
- `NOTION_TOKEN`
- `NOTION_DATABASE_ID`
- `RESUME_MD` — full resume text
- `LABELS_JSONL` — only if you use the live eval workflow

**Variables** (optional cost preset; Best is the default in this repo, Lite in the Pro Kit):
- `RADAR_MODEL_SCORE` = `claude-haiku-5-5` (Lite) or `claude-opus-5-5` (Best)
- `RADAR_MODEL_JUDGE` = same choice

`Radar daily` then runs every morning. Trigger it once by hand (Actions → Radar daily →
Run workflow → dry run) to confirm. The preflight step names any missing or malformed secret
before a cent is spent.

## 6. Build on it with Claude Code (optional)
```bash
export ANTHROPIC_API_KEY=sk-ant-...      # the job-radar key
claude -p "Read CLAUDE.md. Add 8 more company boards to config/companies.yaml and verify them with check-boards." --model claude-opus-5-5
```

## Alternative to step 5: Managed Agents
Console → **Managed Agents → Quickstart** can host a scheduled agent instead of GitHub
Actions. It adds $0.08 per running session-hour on top of tokens and has no batch discount,
so Actions is cheaper for this job.
