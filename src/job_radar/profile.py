"""Who the radar searches for: config/profile.yaml, with defaults that match the original setup.

Prompts, enums and sign-offs read from here instead of hardcoding one person, so a copy of this
repo works for anyone after editing one YAML file (or copying a lane pack over it).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ALWAYS_LOCATIONS = ["Remote-US", "Remote-Global", "Other"]

_DEFAULT_LANE_GUIDANCE = """- They apply to the work, not the title. Senior, Staff, or Lead titles are fine when the job
  matches their lanes. Do not drop the score for seniority alone.
- Score low (Skip) for internships, people-management-only roles, and roles whose core is a
  function they do not target."""

_DEFAULT_RELEVANT_WORK = """- Software QA, test automation, SDET, quality engineering
- AI/LLM evaluation, red-teaming, model QA, data annotation quality"""


@dataclass(frozen=True)
class Profile:
    name: str = "the candidate"
    home: str = "the United States"
    work_country: str = "US"
    in_area: list[str] = field(default_factory=lambda: ["Davis", "Sacramento", "Yolo", "Bay Area"])
    global_remote_ok_lanes: list[str] = field(default_factory=lambda: ["QA/SDET", "AI Eval"])
    lanes: list[str] = field(default_factory=lambda: ["QA/SDET", "AI Eval", "SWE", "Data", "IT Spec", "Research", "Other"])
    resume_versions: list[str] = field(default_factory=lambda: ["QA-SDET", "AI-Eval", "SWE", "General"])
    lane_guidance: str = _DEFAULT_LANE_GUIDANCE
    relevant_work: str = _DEFAULT_RELEVANT_WORK
    title_include: list[str] | None = None
    title_exclude: list[str] | None = None
    location_include: list[str] | None = None
    notion_database_id: str = ""

    @property
    def locations(self) -> list[str]:
        """Location enum for the scorer: named places first, then the fixed remote/other values."""
        return [x for x in self.in_area if x not in ALWAYS_LOCATIONS] + ALWAYS_LOCATIONS

    def render(self, text: str) -> str:
        """Fill {{NAME}}, {{HOME}}, {{COUNTRY}}, {{IN_AREA}}, {{LANE_GUIDANCE}}, {{RELEVANT_WORK}},
        {{LANES}}, {{RESUME_VERSIONS}} in a prompt."""
        subs = {
            "{{NAME}}": self.name,
            "{{HOME}}": self.home,
            "{{COUNTRY}}": self.work_country,
            "{{IN_AREA}}": ", ".join(self.in_area) or "none",
            "{{LANE_GUIDANCE}}": self.lane_guidance.strip(),
            "{{RELEVANT_WORK}}": self.relevant_work.strip(),
            "{{LANES}}": ", ".join(self.lanes),
            "{{RESUME_VERSIONS}}": ", ".join(self.resume_versions),
        }
        for k, v in subs.items():
            text = text.replace(k, v)
        return text


def _as_list(v) -> list[str] | None:
    if v is None:
        return None
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


def load_profile(path: Path | None = None) -> Profile:
    """Read config/profile.yaml (or RADAR_PROFILE_PATH). A missing file means the defaults."""
    path = Path(path or os.getenv("RADAR_PROFILE_PATH") or ROOT / "config" / "profile.yaml")
    if not path.exists():
        return Profile()
    raw = yaml.safe_load(path.read_text()) or {}
    kw = {}
    for k in ("name", "home", "work_country", "lane_guidance", "relevant_work", "notion_database_id"):
        if raw.get(k):
            kw[k] = str(raw[k])
    for k in ("in_area", "global_remote_ok_lanes", "lanes", "resume_versions"):
        if raw.get(k):
            kw[k] = _as_list(raw[k])
    for k in ("title_include", "title_exclude", "location_include"):
        if raw.get(k) is not None:
            kw[k] = _as_list(raw[k])
    p = Profile(**kw)
    if "Other" not in p.lanes:
        p = Profile(**{**kw, "lanes": [*p.lanes, "Other"]})
    return p


PROFILE = load_profile()
