"""Settings loaded from environment variables (and an optional .env file)."""
from __future__ import annotations

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


@dataclass(frozen=True)
class Settings:
    model_filter: str = os.getenv("RADAR_MODEL_FILTER", "claude-haiku-5-5")
    model_score: str = os.getenv("RADAR_MODEL_SCORE", "claude-sonnet-5-5")
    model_judge: str = os.getenv("RADAR_MODEL_JUDGE", "claude-fable-5-1")
    fit_threshold: int = int(os.getenv("RADAR_FIT_THRESHOLD", "70"))
    max_scored: int = int(os.getenv("RADAR_MAX_SCORED", "60"))
    max_run_usd: float = float(os.getenv("RADAR_MAX_RUN_USD", "3.00"))
    notion_token: str = os.getenv("NOTION_TOKEN", "")
    notion_database_id: str = os.getenv(
        "NOTION_DATABASE_ID", "d2c0fa46a231491cbe754cbf5abf272e"
    )
    resume_path: Path = Path(os.getenv("RADAR_RESUME_PATH", ROOT / "data" / "resume.md"))
    companies_path: Path = ROOT / "config" / "companies.yaml"
    prompts_dir: Path = ROOT / "prompts"
    reports_dir: Path = ROOT / "reports"
    costs_path: Path = ROOT / "costs.csv"


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
