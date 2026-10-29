"""Settings loaded from environment variables (and an optional .env file)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.split(" #", 1)[0].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip())


_load_dotenv()

TUNING_PATH = ROOT / "config" / "tuning.json"


def _int(name: str, default: int) -> int:
    """Env int; a blank value (e.g. an empty workflow input) counts as unset."""
    v = (os.getenv(name) or "").strip()
    return int(v) if v else default


def _fit_threshold() -> int:
    """RADAR_FIT_THRESHOLD if set, else the monthly retune's config/tuning.json, else 50."""
    if (os.getenv("RADAR_FIT_THRESHOLD") or "").strip():
        return _int("RADAR_FIT_THRESHOLD", 50)
    try:
        return int(json.loads(TUNING_PATH.read_text())["fit_threshold"])
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return 50


@dataclass(frozen=True)
class Settings:
    model_filter: str = os.getenv("RADAR_MODEL_FILTER", "claude-haiku-5-5")
    model_score: str = os.getenv("RADAR_MODEL_SCORE", "claude-opus-5-5")
    model_judge: str = os.getenv("RADAR_MODEL_JUDGE", "claude-fable-5-1")
    fit_threshold: int = field(default_factory=_fit_threshold)
    # Local (Davis/Sacramento-area) matches are filed at fit_threshold - local_bonus.
    local_bonus: int = _int("RADAR_LOCAL_BONUS", 10)
    # Max share of each run's scoring slots that local postings may take while remote ones wait.
    local_share: float = float((os.getenv("RADAR_LOCAL_SHARE") or "").strip() or "0.6")
    max_scored: int = _int("RADAR_MAX_SCORED", 40)
    max_run_usd: float = float((os.getenv("RADAR_MAX_RUN_USD") or "").strip() or "1.50")
    # Few-shot feedback examples added to the scorer prompt (0 = off).
    few_shot: int = _int("RADAR_FEW_SHOT", 12)
    # Detail-page fetches per run for big national boards (iCIMS).
    icims_max_detail: int = _int("RADAR_ICIMS_MAX_DETAIL", 40)
    notion_token: str = os.getenv("NOTION_TOKEN", "").strip()
    notion_database_id: str = os.getenv(
        "NOTION_DATABASE_ID", "d2c0fa46a231491cbe754cbf5abf272e"
    )
    resume_path: Path = Path(os.getenv("RADAR_RESUME_PATH", ROOT / "data" / "resume.md"))
    companies_path: Path = ROOT / "config" / "companies.yaml"
    prompts_dir: Path = ROOT / "prompts"
    reports_dir: Path = ROOT / "reports"
    costs_path: Path = Path(os.getenv("RADAR_COSTS_PATH", ROOT / "costs.csv"))
    feedback_path: Path = Path(os.getenv("RADAR_FEEDBACK_PATH", ROOT / "data" / "feedback.jsonl"))
    tuning_path: Path = TUNING_PATH
    # Weekly summary page goes under this Notion page (default: Job Search Command Center).
    summary_parent_id: str = (os.getenv("NOTION_SUMMARY_PARENT_ID") or "").strip() or \
        "36cd76e3aa8a81449216c5cd87bc751c"
    monthly_budget_usd: float = float((os.getenv("RADAR_MONTHLY_BUDGET_USD") or "").strip() or "200")
    seen_path: Path = Path(os.getenv("RADAR_SEEN_PATH", ROOT / "data" / "seen.json"))
    seen_days: int = _int("RADAR_SEEN_DAYS", 30)
    workers: int = _int("RADAR_WORKERS", 6)


def load_resume(settings: Settings) -> str:
    """Resume text comes from RESUME_MD (CI secret) or data/resume.md (local, gitignored)."""
    text = os.getenv("RESUME_MD") or (
        settings.resume_path.read_text() if settings.resume_path.exists() else ""
    )
    if not text.strip():
        raise SystemExit(
            "No resume found. Paste your resume into data/resume.md "
            "(local) or set the RESUME_MD secret (GitHub Actions)."
        )
    return text
