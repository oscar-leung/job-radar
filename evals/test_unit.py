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
def test_score_params_force_tool_and_cache_system():
    sys_blocks = sc.system_blocks("rubric", "resume text")
    params = sc.score_params("claude-sonnet-5-5", sys_blocks, P())
    assert params["tool_choice"] == {"type": "tool", "name": "record_fit"}
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
