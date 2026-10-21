import json
"""Offline tests — no API calls, run on every push."""
from types import SimpleNamespace

import pytest

from job_radar.cost import CostMeter, call_cost
from job_radar.ingest import Posting, keyword_prefilter, strip_html
from job_radar.metrics import confusion, precision_recall
from job_radar.notion_out import build_children, build_properties
from job_radar import score as sc


def P(title="SDET II", **kw):
    base = dict(source="Greenhouse", company="acme", external_id="gh:acme:1",
                title=title, location="Remote - US", url="https://x", description="d")
    base.update(kw)
    return Posting(**base)


SCORE = {"fit_score": 82, "fit": "🟡 Stretch", "lane": "QA/SDET", "location": "Remote-US",
         "resume_version": "QA-SDET", "reasons": ["pytest", "Playwright"],
         "gaps": ["Go"], "bullets": ["a", "b", "c"]}


# --- ingest ---------------------------------------------------------------
def test_strip_html_keeps_line_breaks_and_unescapes():
    out = strip_html("&lt;p&gt;Hello&amp;nbsp;<b>world</b>&lt;/p&gt;<li>one</li><li>two</li>")
    assert "Hello" in out and "world" in out and "<" not in out
    assert out.splitlines()[-2:] == ["one", "two"]


@pytest.mark.parametrize("title,keep", [
    ("Senior SDET", True), ("QA Automation Engineer", True), ("LLM Evaluation Specialist", True),
    ("Director of Quality", False), ("Account Executive", False), ("Backend Engineer", False),
])
def test_keyword_prefilter(title, keep):
    inc = ["\\bqa\\b", "quality", "sdet", "automation", "evaluat"]
    exc = ["director", "account executive"]
    assert bool(keyword_prefilter([P(title)], inc, exc)) is keep


# --- cost -----------------------------------------------------------------
def test_call_cost_sonnet_standard_and_batch():
    u = {"input_tokens": 1_000_000, "output_tokens": 100_000}
    assert call_cost("claude-sonnet-5-5", u) == pytest.approx(2.0 + 1.0)
    assert call_cost("claude-sonnet-5-5", u, batch=True) == pytest.approx(1.5)


def test_call_cost_counts_cache_tokens():
    u = SimpleNamespace(input_tokens=0, output_tokens=0,
                        cache_creation_input_tokens=1_000_000, cache_read_input_tokens=1_000_000)
    assert call_cost("claude-opus-5-5", u) == pytest.approx(5.0 + 0.2)


def test_unknown_model_raises():
    with pytest.raises(KeyError):
        call_cost("claude-nope", {})


def test_meter_budget_guard():
    m = CostMeter(budget_usd=0.01)
    m.add("claude-fable-5-1", {"output_tokens": 1000})  # $0.05
    assert m.over_budget and m.calls == 1


# --- scoring request shape ------------------------------------------------
def test_score_params_structured_output_and_cache_system():
    sys_blocks = sc.system_blocks("rubric", "resume text")
    params = sc.score_params("claude-sonnet-5-5", sys_blocks, P())
    assert "tool_choice" not in params and "tools" not in params
    schema = params["output_config"]["format"]["schema"]
    assert params["output_config"]["format"]["type"] == "json_schema"
    assert schema["additionalProperties"] is False
    assert "minimum" not in schema["properties"]["fit_score"]
    assert "maxItems" not in schema["properties"]["bullets"]
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "<resume>" in params["system"][0]["text"]


def test_tool_enums_match_tracker_options():
    props = sc.SCORE_TOOL["input_schema"]["properties"]
    assert set(props["fit"]["enum"]) == {"🟢 Strong", "🟡 Stretch", "🎯 Reach", "⚪ Skip"}
    assert "Remote-US" in props["location"]["enum"]


def test_parse_tool_input():
    msg = SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="record_fit", input=SCORE)])
    assert sc.parse_message(msg, "record_fit")["fit_score"] == 82


# --- notion mapping -------------------------------------------------------
def test_notion_properties_use_tracker_columns():
    props = build_properties(P(), SCORE, "2026-10-15")
    for col in ["Company", "Role / Title", "JD URL", "External ID", "Status", "Fit",
                "Lane / Category", "Location", "Source Platform", "Resume Version", "Notes"]:
        assert col in props
    assert props["Status"]["select"]["name"] == "Not Applied"
    assert props["Notes"]["rich_text"][0]["text"]["content"].startswith("RADAR: 82/100")


def test_notion_children_have_three_bullets():
    kids = build_children(SCORE)
    assert sum(k["type"] == "bulleted_list_item" for k in kids) == 3


# --- metrics --------------------------------------------------------------
def test_precision_recall():
    gold = [True, True, False, False, True]
    pred = [True, False, True, False, True]
    c = confusion(gold, pred)
    assert c == {"tp": 2, "fp": 1, "fn": 1, "tn": 1}
    p, r, f1 = precision_recall(c)
    assert p == pytest.approx(2 / 3) and r == pytest.approx(2 / 3) and f1 == pytest.approx(2 / 3)


def test_precision_recall_handles_empty():
    assert precision_recall({"tp": 0, "fp": 0, "fn": 0, "tn": 3}) == (0.0, 0.0, 0.0)


def test_compact_labels_join_snapshot(tmp_path):
    from conftest import load_labels
    snap = tmp_path / "snap.jsonl"
    snap.write_text(json.dumps({"source": "Greenhouse", "company": "c", "external_id": "gh:c:1",
                                "title": "SDET", "location": "Remote", "url": "u", "description": "d"}) + "\n")
    labs = tmp_path / "labels.jsonl"
    labs.write_text('{"external_id": "gh:c:1", "label": "good", "lane": "QA/SDET"}\n'
                    '{"external_id": "gh:c:missing", "label": "bad"}\n')
    rows = load_labels(labs, snap)
    assert len(rows) == 1 and rows[0]["title"] == "SDET" and rows[0]["label"] == "good"


def test_notion_dedupe_loads_ids_once(monkeypatch):
    from job_radar import notion_out
    calls = []

    class R:
        status_code = 200
        headers = {}
        def __init__(self, payload): self.payload = payload
        def json(self): return self.payload
        def raise_for_status(self): pass

    def fake(method, url, **kw):
        calls.append(url)
        row = {"properties": {"External ID": {"rich_text": [{"plain_text": "gh:x:1"}]}}}
        return R({"results": [row], "has_more": False})

    monkeypatch.setattr(notion_out.requests, "request", fake)
    t = notion_out.NotionTracker("tok", "db")
    assert t.exists("gh:x:1") and not t.exists("gh:x:2") and not t.exists("gh:x:3")
    assert len(calls) == 1


def test_notion_retries_on_429(monkeypatch):
    from job_radar import notion_out
    seq = iter([429, 200])

    class R:
        def __init__(self, code): self.status_code, self.headers = code, {"Retry-After": "0"}
        def raise_for_status(self): pass

    monkeypatch.setattr(notion_out.requests, "request", lambda *a, **k: R(next(seq)))
    monkeypatch.setattr(notion_out.time, "sleep", lambda s: None)
    assert notion_out._request("POST", "u").status_code == 200


def test_parse_structured_text_normalizes():
    out = {**SCORE, "fit_score": 140, "fit": "🟢 strong", "bullets": ["a", "b", "c", "d"]}
    msg = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(out))])
    got = sc.parse_message(msg, "record_fit")
    assert got["fit_score"] == 100 and got["fit"] == "🟢 Strong" and len(got["bullets"]) == 3


def test_refusal_raises():
    msg = SimpleNamespace(stop_reason="refusal", content=[])
    with pytest.raises(ValueError):
        sc.parse_message(msg, "record_fit")


def test_truncated_reply_retried_with_more_room():
    calls = []
    good = json.dumps({"relevant": True, "reason": "QA role"})

    class Client:
        class messages:
            @staticmethod
            def create(**kw):
                calls.append(kw["max_tokens"])
                if len(calls) == 1:
                    return SimpleNamespace(stop_reason="max_tokens", usage={},
                                           content=[SimpleNamespace(type="text", text='{"relevant": true, "reason": "QA')])
                return SimpleNamespace(stop_reason="end_turn", usage={},
                                       content=[SimpleNamespace(type="text", text=good)])

    out, usages = sc.is_relevant(Client, "claude-haiku-5-5", sc.system_blocks("r", "cv"), P())
    assert out["relevant"] is True and len(usages) == 2 and calls[1] == 2 * calls[0]


def test_location_gate():
    from job_radar.__main__ import in_area
    assert in_area({"location": "Remote-US", "lane": "SWE"})
    assert in_area({"location": "Remote-Global", "lane": "QA/SDET"})
    assert not in_area({"location": "Remote-Global", "lane": "SWE"})
    assert not in_area({"location": "Other", "lane": "QA/SDET"})


def test_seen_cache_expires(tmp_path):
    import datetime as dt
    from job_radar.__main__ import load_seen, save_seen
    p = tmp_path / "seen.json"
    save_seen(p, {"old": "2026-08-01", "new": "2026-10-19"})
    got = load_seen(p, 30, today=dt.date(2026, 10, 20))
    assert got == {"new": "2026-10-19"}
    assert load_seen(tmp_path / "missing.json", 30) == {}


def test_location_prefilter():
    from job_radar.ingest import location_prefilter
    mk = lambda loc: Posting(source="Greenhouse", company="c", external_id=loc or "blank", title="SDET",
                             location=loc, url="u", description="d")
    pats = ["remote", "california|\\bCA\\b", "davis|sacramento", "mountain view"]
    kept = {p.location for p in location_prefilter(
        [mk("Remote - US"), mk("Hawthorne, CA"), mk("Austin, TX"), mk("London"), mk(""), mk("Mountain View")], pats)}
    assert kept == {"Remote - US", "Hawthorne, CA", "", "Mountain View"}


# --- application kits ----------------------------------------------------
def test_radar_score_parse():
    from job_radar.kits import radar_score
    assert radar_score("RADAR: 90/100 (2026-10-20). Why: x") == 90
    assert radar_score("Applied via LinkedIn") == -1


def test_kit_schema_is_strict_everywhere():
    from job_radar.kits import KIT_SCHEMA, JUDGE_SCHEMA
    j = sc.json_schema(JUDGE_SCHEMA)
    assert j["additionalProperties"] is False
    assert j["properties"]["unsupported"]["items"]["additionalProperties"] is False
    assert sc.json_schema(KIT_SCHEMA)["additionalProperties"] is False


def test_kit_markdown_flags_unsupported_claims():
    from job_radar.kits import kit_markdown
    kit = {"summary": "s", "bullets": ["b1"], "skills_to_feature": ["pytest"], "cover_note": "p1\n\np2",
           "why_company": "w", "gaps": [], "interview_topics": ["t"]}
    clean = kit_markdown(kit, {"verdict": "clean", "unsupported": []})
    assert any("✅ clean" in l for l in clean) and "### Gaps and how to address them" not in clean
    bad = kit_markdown(kit, {"verdict": "major", "unsupported": [{"quote": "10 years", "why": "not in resume"}]})
    assert any("10 years" in l for l in bad) and any("⚠️ major" in l for l in bad)


def test_fetch_jd_greenhouse(monkeypatch):
    from job_radar import kits
    class R:
        def json(self): return {"content": "&lt;p&gt;Build pytest suites&lt;/p&gt;"}
    monkeypatch.setattr(kits.requests, "get", lambda url, **k: R())
    assert "Build pytest suites" in kits.fetch_jd("gh:acme:123")


def test_run_loop_parallel_saves_progress(tmp_path, monkeypatch):
    """cmd_run with fake APIs: filters, scores in parallel, files only in-area matches, saves seen."""
    import sys, types
    from job_radar import __main__ as m
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=lambda: object()))
    posts = [P(external_id=f"gh:x:{i}", title="SDET", location="Remote") for i in range(10)]
    monkeypatch.setattr(m, "fetch_all", lambda cfg: posts)
    monkeypatch.setattr(m, "load_resume", lambda s: "cv")
    monkeypatch.setattr(m, "_companies", lambda s: {"greenhouse": ["x"]})
    monkeypatch.setattr(m.sc, "is_relevant", lambda c, mo, sy, p: ({"relevant": p.external_id != "gh:x:0"}, [{}]))
    def fake_score(c, mo, sy, p):
        i = int(p.external_id.rsplit(":", 1)[1])
        return {**SCORE, "fit_score": 40 + i * 5, "location": "Remote-US" if i % 2 else "Other"}, [{}]
    monkeypatch.setattr(m.sc, "score", fake_score)
    filed = []
    class T:
        def __init__(self, *a): pass
        def exists(self, x): return False
        def add(self, p, s): filed.append(p.external_id); return "url"
    import job_radar.notion_out as no
    monkeypatch.setattr(no, "NotionTracker", T)
    s = m.Settings()
    for k, v in dict(seen_path=tmp_path / "seen.json", reports_dir=tmp_path, costs_path=tmp_path / "c.csv",
                     workers=3, fit_threshold=50, max_scored=100).items():
        object.__setattr__(s, k, v)
    m.cmd_run(s, dry_run=False, limit=None)
    assert sorted(filed) == ["gh:x:3", "gh:x:5", "gh:x:7", "gh:x:9"]   # >=50 and Remote-US
    assert len(json.loads((tmp_path / "seen.json").read_text())) == 10


def test_prep_markdown_and_schema():
    from job_radar.prep import PREP_SCHEMA, prep_markdown
    sch = sc.json_schema(PREP_SCHEMA)
    assert sch["properties"]["likely_questions"]["items"]["additionalProperties"] is False
    p = {"role_decoded": ["r"], "likely_questions": [{"question": "q?", "answer": "a1\na2"}],
         "topics_to_review": ["t"], "questions_to_ask": ["k"], "first_90_days": "plan",
         "linkedin_note": "Hi [Name], ...", "hiring_manager_email": "Subject: x\nHi [Name],\nOscar Leung"}
    lines = prep_markdown(p, {"verdict": "clean", "unsupported": []})
    assert "- Q1. q?" in lines and "a2" in lines and any("✅" in l for l in lines)


def test_effort_in_output_config():
    cfg = sc.output_config(sc.SCORE_TOOL, "high")
    assert cfg["effort"] == "high" and cfg["format"]["type"] == "json_schema"
    assert "effort" not in sc.output_config(sc.SCORE_TOOL)
    assert sc.score_params("claude-opus-5-5", [], P(), "xhigh")["max_tokens"] >= 16000


def test_workday_fetch_and_hydrate(monkeypatch):
    from job_radar import ingest
    class R:
        def __init__(self, data): self.data = data
        def raise_for_status(self): pass
        def json(self): return self.data
    def post(url, json=None, **k):
        jobs = [{"title": "QA Analyst", "externalPath": "/job/Sacramento/QA_R-1", "locationsText": "Sacramento"}] \
            if json["offset"] == 0 else []
        return R({"jobPostings": jobs})
    monkeypatch.setattr(ingest.requests, "post", post)
    monkeypatch.setattr(ingest.requests, "get",
                        lambda url, **k: R({"jobPostingInfo": {"jobDescription": "<p>Test EHR releases</p>"}}))
    ps = ingest.fetch_workday("sutterhealth|wd1|SH", terms=["QA", "test"])
    assert len(ps) == 1 and ps[0].external_id == "wd:sutterhealth|wd1|SH:/job/Sacramento/QA_R-1"
    assert ps[0].url == "https://sutterhealth.wd1.myworkdayjobs.com/SH/job/Sacramento/QA_R-1"
    out = ingest.hydrate_workday(ps)
    assert "Test EHR releases" in out[0].description


@pytest.mark.parametrize("loc,blocked", [
    ("Canada; Calgary; Toronto; Vancouver", True),
    ("Remote Canada", True),
    ("Remote - UK", True),
    ("Bangalore, India", True),
    ("Remote US", False),
    ("Remote - USA", False),
    ("San Francisco, CA; Toronto, Canada", False),
    ("Remote (USA or Canada)", False),
    ("Anywhere", False),
    ("", False),
])
def test_non_us_only(loc, blocked):
    from job_radar.ingest import non_us_only
    assert non_us_only(loc) is blocked


def test_location_prefilter_drops_non_us_remote():
    from job_radar.ingest import location_prefilter
    kept = location_prefilter([P(location="Remote Canada"), P(location="Remote - US")], ["remote"])
    assert [p.location for p in kept] == ["Remote - US"]
