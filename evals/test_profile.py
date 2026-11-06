"""Offline tests: profile loading, prompt rendering, Notion bootstrap schema."""
from pathlib import Path

import yaml

from job_radar import notion_out as no
from job_radar import profile as pr
from job_radar import score as sc


def test_default_profile_matches_original_enums():
    p = pr.Profile()
    assert p.locations == ["Davis", "Sacramento", "Yolo", "Bay Area", "Remote-US", "Remote-Global", "Other"]
    assert p.lanes[-1] == "Other"


def test_load_profile_from_yaml(tmp_path: Path):
    f = tmp_path / "profile.yaml"
    f.write_text(yaml.safe_dump({
        "name": "Ada Lovelace", "home": "Austin, TX", "in_area": ["Austin", "Remote-US"],
        "lanes": ["Data", "SWE"], "title_include": ["data"], "notion_database_id": "abc",
    }))
    p = pr.load_profile(f)
    assert p.name == "Ada Lovelace"
    assert p.locations == ["Austin", "Remote-US", "Remote-Global", "Other"]  # no duplicates
    assert p.lanes == ["Data", "SWE", "Other"]  # Other is always present
    assert p.title_include == ["data"] and p.title_exclude is None
    assert p.notion_database_id == "abc"


def test_missing_profile_file_gives_defaults(tmp_path: Path):
    assert pr.load_profile(tmp_path / "nope.yaml") == pr.Profile()


def test_render_fills_every_placeholder():
    p = pr.Profile(name="Ada Lovelace", home="Austin, TX", in_area=["Austin"], lane_guidance="- guidance")
    out = p.render("{{NAME}}|{{HOME}}|{{COUNTRY}}|{{IN_AREA}}|{{LANE_GUIDANCE}}|{{LANES}}|{{RESUME_VERSIONS}}|{{RELEVANT_WORK}}")
    assert "{{" not in out
    assert out.startswith("Ada Lovelace|Austin, TX|US|Austin|- guidance|")


def test_shipped_prompts_render_without_leftover_placeholders():
    for name in ("filter_rubric.md", "scoring_rubric.md"):
        text = sc.load_prompt(pr.ROOT / "prompts", name)
        assert "{{" not in text, name
    assert "{{" not in pr.PROFILE.render(__import__("job_radar.kits", fromlist=["KIT_SYSTEM"]).KIT_SYSTEM)


def test_score_enums_come_from_profile():
    assert sc.LOCATIONS == pr.PROFILE.locations
    assert sc.SCORE_TOOL["input_schema"]["properties"]["location"]["enum"] == sc.LOCATIONS


def test_tracker_schema_covers_everything_the_radar_writes():
    from job_radar.ingest import Posting
    p = Posting("Greenhouse", "acme", "gh:acme:1", "QA", "Remote", "u", "d")
    s = {"fit_score": 80, "fit": "🟡 Stretch", "lane": "QA/SDET", "location": "Remote-US",
         "resume_version": "General", "reasons": [], "gaps": [], "bullets": ["a", "b", "c"]}
    written = set(no.build_properties(p, s, "2026-01-01")) | {"Cover Letter Status"}
    schema = no.tracker_schema()
    assert written <= set(schema)
    assert schema["Company"] == {"title": {}}
    assert set(no.REQUIRED_PROPERTIES) <= set(schema)


def test_missing_properties_reports_name_and_type():
    props = {k: {"type": v} for k, v in no.REQUIRED_PROPERTIES.items()}
    assert no.missing_properties(props) == []
    props["Fit"] = {"type": "rich_text"}
    del props["Notes"]
    out = no.missing_properties(props)
    assert "Fit (select), found rich_text" in out and "Notes (rich_text)" in out


def test_page_id_from_url_or_id():
    url = "https://www.notion.so/me/My-Page-3ccd76e3aa8a819bad01ee83a0e22459?pvs=4"
    assert no.page_id_from(url) == "3ccd76e3-aa8a-819b-ad01-ee83a0e22459"
    assert no.page_id_from("3ccd76e3aa8a819bad01ee83a0e22459") == "3ccd76e3-aa8a-819b-ad01-ee83a0e22459"
