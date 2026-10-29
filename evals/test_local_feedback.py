"""Offline tests for v3: local-first ranking, polite HTTP (Workday 429s), new local sources,
the Status feedback loop + retune, and the weekly summary. No network, no API calls."""
import datetime as dt
import json
import threading
import time
from types import SimpleNamespace

import pytest

from job_radar import feedback as fb
from job_radar import http, ingest, locality
from job_radar.ingest import Posting


def P(title="SDET", location="Remote - US", source="Greenhouse", eid=None, **kw):
    return Posting(source=source, company=kw.get("company", "acme"),
                   external_id=eid or f"gh:acme:{title}:{location}", title=title,
                   location=location, url=kw.get("url", "https://x"), description=kw.get("description", "d"))


class Resp:
    def __init__(self, code=200, data=None, text="", headers=None):
        self.status_code, self._data, self.text, self.headers = code, data, text, headers or {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}")


# --- 1. local-first ---------------------------------------------------------------------
@pytest.mark.parametrize("loc,tier", [
    ("Davis, CA", "davis"), ("West Sacramento, CA", "davis"), ("Woodland, CA (Yolo County)", "davis"),
    ("Sacramento, CA", "sacramento"), ("Roseville, CA", "sacramento"), ("Rancho Cordova", "sacramento"),
    ("Sacramento, CA or Remote", "sacramento"), ("San Francisco, CA (Hybrid)", "hybrid"),
    ("Remote - US", "remote"), ("Mountain View, CA", "bay_area"), ("Austin, TX", "other"), ("", "other"),
])
def test_tier_from_text(loc, tier):
    assert locality.tier_from_text(loc) == tier


def test_tier_after_scoring_uses_scorer_and_hybrid_text():
    assert locality.tier_for({"location": "Remote-US"}, "United States") == "remote"
    assert locality.tier_for({"location": "Bay Area"}, "San Jose, CA",
                             "This is a hybrid role, in office once a week") == "hybrid"
    assert locality.tier_for({"location": "Remote-US"}, "Sacramento, CA") == "sacramento"
    assert locality.tier_for({"location": "Davis"}, "") == "davis"


def test_sort_local_first_is_stable_and_respects_priority():
    ps = [P("a", "Remote"), P("b", "Austin, TX"), P("c", "Sacramento, CA"), P("d", "Davis, CA"),
          P("e", "Remote"), P("f", "Hybrid - Oakland")]
    assert [p.title for p in locality.sort_local_first(ps)] == ["d", "c", "f", "a", "e", "b"]
    alt = ["davis", "sacramento", "remote", "hybrid", "bay_area", "other"]
    assert [p.title for p in locality.sort_local_first(ps, alt)] == ["d", "c", "a", "e", "f", "b"]


def test_local_cutoff_bonus():
    assert locality.cutoff_for("davis", 50, 10) == 40
    assert locality.cutoff_for("sacramento", 50, 10) == 40
    assert locality.cutoff_for("remote", 50, 10) == 50


def test_notes_tag_local_and_still_parse():
    from job_radar.kits import radar_score
    from job_radar.notion_out import build_properties
    s = {"fit_score": 44, "fit": "🎯 Reach", "lane": "IT Spec", "location": "Davis",
         "resume_version": "General", "reasons": ["r"], "gaps": [], "bullets": ["a", "b", "c"]}
    notes = build_properties(P(location="Davis, CA"), s, "2026-10-29", "davis")["Notes"]["rich_text"][0]["text"]["content"]
    assert notes.startswith("RADAR: 44/100 (2026-10-29). 📍 Local: Davis/Yolo.")
    assert radar_score(notes) == 44 and fb.parse_notes(notes) == (44, "2026-10-29")
    plain = build_properties(P(), {**s, "location": "Remote-US"}, "2026-10-29", "remote")
    assert "📍" not in plain["Notes"]["rich_text"][0]["text"]["content"]


def test_run_files_local_match_below_global_cutoff(tmp_path, monkeypatch):
    """A Davis posting at 44 is filed (local cutoff 40); a remote one at 44 is not; local is scored first."""
    import sys, types
    from job_radar import __main__ as m
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=lambda: object()))
    posts = [P("SDET", "Remote - US", eid="gh:x:1"), P("IT Specialist", "Davis, CA", eid="gh:x:2")]
    monkeypatch.setattr(m, "fetch_all", lambda cfg: posts)
    monkeypatch.setattr(m, "load_resume", lambda s: "cv")
    monkeypatch.setattr(m, "_companies", lambda s: {"greenhouse": ["x"]})
    monkeypatch.setattr(m.sc, "is_relevant", lambda c, mo, sy, p: ({"relevant": True}, [{}]))
    order = []

    def fake_score(c, mo, sy, p):
        order.append(p.external_id)
        loc = "Davis" if "Davis" in p.location else "Remote-US"
        return {"fit_score": 44, "fit": "🎯 Reach", "lane": "IT Spec", "location": loc,
                "resume_version": "General", "reasons": [], "gaps": [], "bullets": ["a", "b", "c"]}, [{}]
    monkeypatch.setattr(m.sc, "score", fake_score)
    filed = []

    class T:
        def __init__(self, *a): pass
        def exists(self, x): return False
        def add(self, p, s, tier=None): filed.append((p.external_id, tier)); return "url"
        def feedback_rows(self): return []
        text = staticmethod(lambda r, n: "")
        select = staticmethod(lambda r, n: "")
    import job_radar.notion_out as no
    monkeypatch.setattr(no, "NotionTracker", T)
    s = m.Settings()
    for k, v in dict(seen_path=tmp_path / "seen.json", reports_dir=tmp_path, costs_path=tmp_path / "c.csv",
                     feedback_path=tmp_path / "fb.jsonl", workers=1, fit_threshold=50, local_bonus=10,
                     max_scored=10).items():
        object.__setattr__(s, k, v)
    m.cmd_run(s, dry_run=False, limit=None)
    assert order[0] == "gh:x:2"                      # local scored first
    assert filed == [("gh:x:2", "davis")]


# --- 3. polite HTTP / Workday 429s ------------------------------------------------------
def test_backoff_grows_with_jitter_and_honors_retry_after():
    lo = [http.backoff_delay(a, base=1, cap=30, rand=lambda: 0.0) for a in range(5)]
    hi = [http.backoff_delay(a, base=1, cap=30, rand=lambda: 1.0) for a in range(5)]
    assert lo == [0.5, 1, 2, 4, 8] and hi == [1, 2, 4, 8, 16]
    assert http.backoff_delay(10, base=1, cap=30, rand=lambda: 1.0) == 30          # capped
    assert http.backoff_delay(0, rand=lambda: 0.0, retry_after="7") == 7           # server wins
    assert http.backoff_delay(0, rand=lambda: 0.0, retry_after="junk") == 0.5


def test_request_retries_429_then_succeeds(monkeypatch):
    codes = iter([429, 503, 200])
    monkeypatch.setattr(http.requests, "post", lambda url, **k: Resp(next(codes), {"ok": 1},
                                                                       headers={"Retry-After": "0"}))
    sleeps = []
    r = http.request("POST", "https://t.wd5.myworkdayjobs.com/x", sleep=sleeps.append, base=1)
    assert r.json() == {"ok": 1} and len(sleeps) == 2 and sleeps[1] >= sleeps[0] / 2


def test_request_gives_up_after_max_attempts(monkeypatch):
    import requests
    monkeypatch.setattr(http.requests, "get", lambda url, **k: Resp(429))
    sleeps = []
    with pytest.raises(requests.HTTPError):
        http.request("GET", "u", max_attempts=3, sleep=sleeps.append)
    assert len(sleeps) == 2


def test_request_retries_connection_errors(monkeypatch):
    import requests
    seq = iter([requests.ConnectionError("reset"), Resp(200, {"ok": 2})])

    def get(url, **k):
        x = next(seq)
        if isinstance(x, Exception):
            raise x
        return x
    monkeypatch.setattr(http.requests, "get", get)
    assert http.request("GET", "u", sleep=lambda s: None).json() == {"ok": 2}


def test_host_limiter_caps_concurrency_and_spaces_requests():
    lim = http.HostLimiter(workers=2, min_interval=0.02)
    live, peak, starts = [0], [0], []
    lock = threading.Lock()

    def work():
        with lim:
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
                starts.append(time.monotonic())
            time.sleep(0.03)
            with lock:
                live[0] -= 1
    ts = [threading.Thread(target=work) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    starts.sort()
    assert peak[0] <= 2
    assert all(b - a >= 0.015 for a, b in zip(starts, starts[1:]))


def test_workday_uses_shared_limiter_and_own_pool(monkeypatch):
    """Workday boards run in their own small pool, separate from the other ATS boards."""
    seen_threads = {"workday": set(), "other": set()}
    monkeypatch.setitem(ingest.FETCHERS, "workday", lambda s: seen_threads["workday"].add(
        threading.current_thread().name) or [])
    monkeypatch.setitem(ingest.FETCHERS, "greenhouse", lambda s: seen_threads["other"].add(
        threading.current_thread().name) or [])
    ingest.fetch_all({"workday": [f"t{i}|wd1|S" for i in range(8)], "greenhouse": ["a", "b"]},
                     workday_workers=2)
    assert len(seen_threads["workday"]) <= 2
    assert not (seen_threads["workday"] & seen_threads["other"])


# --- 4. new sources ---------------------------------------------------------------------
UC_PAGE = """<main><div class="jobspot"> <h2 class="lookslike-h3"> <a href="https://careerspub.universityofcalifornia.edu/psc/ucdmed/EMPLOYEE/HRMS/c/X?Page=HRS_APP_JBPST_FL&amp;JobOpeningId=89596&amp;PostingSeq=1" class="jtitle" onclick="x">Epic Analyst &amp; Tester</a> </h2>
<div class="jloc lookslike-h4">Davis Health System</div> <div class="jtype lookslike-h4">Full Time</div>
<div class="jfamily lookslike-h4"> Category: Information Technology </div> <div class="jclose lookslike-h4">Posting Date: 10/27/2026</div>
<div class="job_work_location lookslike-h4">Work Location: Sacramento</div> <p class="jdesc lookslike-h4">Builds and tests Epic releases...</p></div>
<div class="jobspot"><h2><a href="https://careerspub.universityofcalifornia.edu/psc/ucdavis/X?JobOpeningId=89572&amp;S=1" class="jtitle">APS Program Analyst</a></h2>
<div class="jloc lookslike-h4">Davis</div></div></main>"""


def test_parse_uc_results():
    ps = ingest.parse_uc_results(UC_PAGE)
    assert [p.external_id for p in ps] == ["uc:ucdmed:89596", "uc:ucdavis:89572"]
    a, b = ps
    assert a.title == "Epic Analyst & Tester" and a.source == "UC Davis Health"
    assert a.location == "Sacramento, CA" and "Epic releases" in a.description
    assert "Information Technology" in a.description and "&amp;" not in a.url
    assert b.source == "UC Davis Campus" and b.location == "Davis, CA"
    assert locality.tier_from_text(a.location) == "sacramento"


def test_fetch_edjoin_maps_fields_and_pages(monkeypatch):
    calls = []

    def get(url, params=None, **k):
        calls.append(params["page"])
        rows = [{"postingID": 2290001 + params["page"], "positionTitle": " Certificated Substitute Teacher ",
                 "districtName": "Davis Joint Unified School District", "city": "Davis", "countyName": "Yolo",
                 "jobType": "Substitute Teacher", "FullTimePartTime": "Part Time", "SingleRate": "$210",
                 "SingleRateDropdown": "Daily", "postingDate": "/Date(1793232000000)/"}]
        return Resp(200, {"totalPages": 2, "data": rows})
    monkeypatch.setattr(http.requests, "get", get)
    monkeypatch.setitem(http.LIMITERS, "edjoin", http.HostLimiter(2))
    ps = ingest.fetch_edjoin("58")
    assert calls == [1, 2] and len(ps) == 2
    p = ps[0]
    assert p.source == "EDJOIN" and p.external_id == "ed:edjoin:2290002"
    assert p.title == "Certificated Substitute Teacher" and p.location == "Davis, CA (Yolo County)"
    assert p.url.endswith("/Home/JobPosting/2290002") and "$210 Daily" in p._summary


def test_edjoin_title_filter_keeps_subs_and_it_drops_aides():
    import yaml
    from job_radar.config import Settings
    cfg = yaml.safe_load(Settings().companies_path.read_text())
    titles = {"Certificated Substitute Teacher Eligibility Pool": True, "IT Specialist II": True,
              "Computer Technician": True, "Substitute SPED Paraprofessional": False,
              "Bilingual Instructional Aide (Spanish)": False, "Head Coach - Wrestling": False,
              "Substitute Custodian": False}
    kept = {p.title for p in ingest.keyword_prefilter([P(t) for t in titles],
                                                      cfg["title_include"], cfg["title_exclude"])}
    assert kept == {t for t, keep in titles.items() if keep}


def test_smartrecruiters_listing_and_detail(monkeypatch):
    def get(url, params=None, **k):
        if url.endswith("/postings"):
            return Resp(200, {"content": [
                {"id": "744", "name": "QA Engineer", "location": {"city": "Roseville", "region": "CA", "remote": False}},
                {"id": "745", "name": "SDET", "location": {"city": "", "region": "", "remote": True}}]})
        return Resp(200, {"jobAd": {"sections": {"jobDescription": {"text": "<p>Write pytest</p>"},
                                                  "qualifications": {"text": "<ul><li>Python</li></ul>"}}}})
    monkeypatch.setattr(http.requests, "get", get)
    ps = ingest.fetch_smartrecruiters("BoschGroup")
    assert [(p.external_id, p.location) for p in ps] == [("sr:BoschGroup:744", "Roseville, CA"),
                                                          ("sr:BoschGroup:745", "Remote - US")]
    out = ingest.hydrate(ps)
    assert "Write pytest" in out[0].description and "Python" in out[0].description


def test_icims_sitemap_and_jsonld_with_location_recheck(monkeypatch):
    sitemap = ("<urlset><url><loc>https://careers-x.icims.com/jobs/1/qa-analyst/job</loc></url>"
               "<url><loc>https://careers-x.icims.com/jobs/2/it-analyst-ii/job</loc></url></urlset>")
    pages = {
        "1": {"@type": "JobPosting", "title": "QA Analyst", "description": "<p>Test EHR</p>",
              "jobLocation": [{"address": {"addressLocality": "Sacramento", "addressRegion": "CA"}}]},
        "2": {"@type": "JobPosting", "title": "IT Analyst II", "description": "<p>x</p>",
              "jobLocation": [{"address": {"addressLocality": "Phoenix", "addressRegion": "AZ"}}]},
    }

    def get(url, **k):
        if url.endswith("sitemap.xml"):
            return Resp(200, text=sitemap)
        jid = url.split("/jobs/")[1].split("/")[0]
        return Resp(200, text=f'<script type="application/ld+json">{json.dumps(pages[jid])}</script>')
    monkeypatch.setattr(http.requests, "get", get)
    ps = ingest.fetch_icims("careers-x")
    assert [p.title for p in ps] == ["Qa Analyst", "It Analyst Ii"] and ps[0].location == ""
    rejected, caps = [], {"iCIMS": 5}
    out = ingest.hydrate(ps, caps, ["remote", "california|\\bCA\\b"], rejected)
    assert [(p.title, p.location) for p in out] == [("QA Analyst", "Sacramento, CA")]
    assert [p.external_id for p in rejected] == ["ic:careers-x:2"] and caps["iCIMS"] == 3


def test_hydrate_cap_skips_extra_detail_fetches(monkeypatch):
    monkeypatch.setattr(ingest, "_detail", lambda p: (setattr(p, "description", "x") or p))
    ps = [P(f"t{i}", source="iCIMS", eid=f"ic:h:{i}", description="") for i in range(5)]
    caps = {"iCIMS": 2}
    assert len(ingest.hydrate(ps, caps)) == 2 and caps["iCIMS"] == 0


def test_select_queue_hydrates_only_up_to_cap(monkeypatch):
    from job_radar import __main__ as m
    fetched = []

    def fake_hydrate(batch, caps, loc, rejected):
        fetched.extend(p.title for p in batch)
        return batch
    monkeypatch.setattr(m, "hydrate", fake_hydrate)
    pre = [P(f"r{i}", "Remote") for i in range(30)] + [P("local", "Davis, CA")]
    q = m.select_queue(pre, 3, m.Settings(), {}, {}, "2026-10-29")
    assert [p.title for p in q] == ["local", "r0", "r1"] and len(fetched) <= 8


# --- 2. feedback loop + retune ----------------------------------------------------------
@pytest.mark.parametrize("status,days,label", [
    ("Applied", None, "applied"), ("Interview Scheduled", None, "interview"), ("Offer", None, "interview"),
    ("Rejected", None, "applied"), ("Skipped – commute", None, "skip_loc"), ("Skipped – onsite", None, "skip_loc"),
    ("Not Interested", None, "skip"), ("Skipped – not a fit", None, "skip"),
    ("Not Applied", 5, None), ("Not Applied", 30, "ignored"), ("", None, None),
    ("SOQ ready – needs CalCareers login to submit", None, "applied"),
])
def test_label_for_status(status, days, label):
    assert fb.label_for_status(status, days) == label


def _rows(spec):
    """spec: (status, score, filed, title, location) -> fake Notion rows + accessors."""
    rows = [{"status": st, "Notes": f"RADAR: {sc}/100 ({d}). Why: x", "Role / Title": t, "Company": "c",
             "External ID": f"gh:c:{i}", "Location": loc, "Lane / Category": "QA/SDET",
             "properties": {"JD URL": {"url": f"https://j/{i}"}}}
            for i, (st, sc, d, t, loc) in enumerate(spec)]
    text = lambda r, n: r.get(n, "")
    select = lambda r, n: r["status"] if n == "Status" else r.get(n, "")
    return rows, text, select


def test_from_rows_and_few_shot_block():
    rows, text, select = _rows([
        ("Applied", 72, "2026-10-20", "SDET II", "Remote-US"),
        ("Interview Scheduled", 81, "2026-10-21", "QA Analyst", "Sacramento"),
        ("Not Interested", 64, "2026-10-22", "Data Engineer", "Remote-US"),
        ("Skipped – commute", 70, "2026-10-22", "QA Lead", "Bay Area"),
        ("Not Applied", 55, "2026-10-28", "Support Eng", "Remote-US"),
        ("Not Applied", 52, "2026-09-01", "Analyst I", "Other"),
    ])
    rows.append({"status": "Applied", "Notes": "Applied via LinkedIn", "properties": {}})   # not Radar
    items = fb.from_rows(rows, text, select, today=dt.date(2026, 10, 29))
    assert [f.label for f in items] == ["applied", "interview", "skip", "skip_loc", "ignored"]
    block = fb.few_shot_block(items, 4)
    assert block.index("INTERVIEWING: QA Analyst") < block.index("APPLIED: SDET II")
    assert "NOT INTERESTED: Data Engineer" in block and "QA Lead" not in block   # location skips excluded
    assert fb.few_shot_block([], 12) == "" and fb.few_shot_block(items, 0) == ""


def test_feedback_roundtrip(tmp_path):
    items = [fb.Feedback("gh:c:1", "applied", "Applied", 70, "c", "SDET", "Remote-US", "QA/SDET", "2026-10-20")]
    fb.save(tmp_path / "f.jsonl", items)
    assert fb.load(tmp_path / "f.jsonl") == items and fb.load(tmp_path / "missing.jsonl") == []


def test_few_shot_goes_into_cached_system_block():
    from job_radar import score as sc
    blocks = sc.system_blocks("RUBRIC", "CV", "## Oscar's recent decisions\n- APPLIED: x")
    assert len(blocks) == 1 and blocks[0]["cache_control"] == {"type": "ephemeral"}
    t = blocks[0]["text"]
    assert t.index("RUBRIC") < t.index("APPLIED: x") < t.index("<resume>")
    assert sc.system_blocks("R", "CV")[0]["text"] == "R\n\n<resume>\nCV\n</resume>"


def _items(pairs):
    return [fb.Feedback(f"x:{i}", lab, lab, sc, "c", "t", "Remote-US", "QA/SDET", "2026-10-01")
            for i, (sc, lab) in enumerate(pairs)]


def test_retune_raises_cutoff_when_low_scores_are_ignored():
    pairs = [(52, "ignored")] * 8 + [(55, "skip")] * 4 + [(58, "ignored")] * 4 + \
            [(60, "applied")] * 1 + [(68, "applied")] * 5 + [(80, "interview")] * 4
    rec = fb.recommend(_items(pairs), current=50)
    assert rec["recommended"] == 60 and rec["precision_after"] > rec["precision_before"]
    assert rec["recall_after"] == 1.0


def test_retune_moves_at_most_max_step_and_never_lowers():
    pairs = [(52, "ignored")] * 20 + [(90, "applied")] * 5
    assert fb.recommend(_items(pairs), current=50, max_step=10)["recommended"] == 60
    assert fb.recommend(_items([(52, "applied")] * 25), current=50)["recommended"] == 50


def test_retune_needs_enough_labels():
    rec = fb.recommend(_items([(70, "applied")] * 5), current=50)
    assert rec["recommended"] == 50 and "keeping" in rec["reason"]


def test_retune_ignores_location_skips():
    pairs = [(55, "skip_loc")] * 30 + [(55, "applied")] * 3
    rec = fb.recommend(_items(pairs), current=50)
    assert rec["n_labels"] == 3 and rec["recommended"] == 50


def test_retune_markdown_and_tuning_file(tmp_path, monkeypatch):
    from job_radar import __main__ as m
    pairs = [(52, "ignored")] * 10 + [(55, "skip")] * 5 + [(70, "applied")] * 6
    s = m.Settings()
    for k, v in dict(feedback_path=tmp_path / "fb.jsonl", tuning_path=tmp_path / "tuning.json",
                     reports_dir=tmp_path, fit_threshold=50).items():
        object.__setattr__(s, k, v)
    fb.save(s.feedback_path, _items(pairs))
    m.cmd_retune(s, write=True, offline=True)
    t = json.loads((tmp_path / "tuning.json").read_text())
    assert t["fit_threshold"] > 50 and t["previous"] == 50 and t["history"][-1]["fit_threshold"] == t["fit_threshold"]
    md = next(tmp_path.glob("retune_*.md")).read_text()
    assert "| Cutoff | Kept |" in md and "cannot justify lowering" in md


def test_settings_threshold_precedence(tmp_path, monkeypatch):
    from job_radar import config
    tp = tmp_path / "tuning.json"
    tp.write_text('{"fit_threshold": 58}')
    monkeypatch.setattr(config, "TUNING_PATH", tp)
    monkeypatch.setenv("RADAR_FIT_THRESHOLD", "")          # blank workflow input = unset
    assert config._fit_threshold() == 58
    monkeypatch.setenv("RADAR_FIT_THRESHOLD", "65")
    assert config._fit_threshold() == 65
    monkeypatch.delenv("RADAR_FIT_THRESHOLD")
    tp.unlink()
    assert config._fit_threshold() == 50


# --- 5. weekly summary ------------------------------------------------------------------
def test_spend_window_and_month_to_date(tmp_path):
    from job_radar.weekly import spend
    p = tmp_path / "costs.csv"
    p.write_text("timestamp,label,calls,usd,by_model\n"
                 "2026-09-30T15:00:00+00:00,run,80,1.40,\n"
                 "2026-10-01T15:00:00+00:00,run,80,1.20,\n"
                 "2026-10-27T15:00:00+00:00,run,80,1.10,\n"
                 "2026-10-28T15:00:00+00:00,kits,20,2.00,\n")
    out = spend(p, dt.date(2026, 10, 23), dt.date(2026, 10, 29))
    assert out["week_total"] == pytest.approx(3.10) and out["runs"] == 2
    assert out["by_label"] == {"run": pytest.approx(1.10), "kits": pytest.approx(2.0)}
    assert out["mtd"] == pytest.approx(4.30)
    assert spend(tmp_path / "missing.csv", dt.date(2026, 10, 1), dt.date(2026, 10, 7))["week_total"] == 0


def test_weekly_build_orders_local_first(tmp_path):
    from job_radar import weekly
    from job_radar.config import Settings

    def row(company, score, loc, status="Not Applied", applied=False):
        return {"properties": {"JD URL": {"url": f"https://{company}"}},
                "_": {"Company": company, "Role / Title": "QA", "Notes": f"RADAR: {score}/100 (2026-10-27). x",
                      "Location": loc, "Fit": "🟡 Stretch", "Lane / Category": "QA/SDET",
                      "Source Platform": "Greenhouse", "Status": status}}

    new = [row("remoteco", 90, "Remote-US"), row("davisco", 45, "Davis"), row("sacco", 70, "Sacramento")]
    applied = [row("sacco", 70, "Sacramento", "Applied")]
    tracker = SimpleNamespace(
        text=lambda r, n: r["_"].get(n, ""), select=lambda r, n: r["_"].get(n, ""),
        radar_rows_since=lambda since: new, applied_since=lambda since: applied,
        feedback_rows=lambda: new + applied)
    s = Settings()
    object.__setattr__(s, "costs_path", tmp_path / "none.csv")
    title, lines = weekly.build(tracker, s, today=dt.date(2026, 11, 1))
    assert title == "Job Radar weekly — Oct 26–Nov 1, 2026"
    matches = [l for l in lines if l.startswith("- ") and "RADAR" not in l and " · " in l and "QA/SDET" in l]
    assert [l.split(" · ")[1].split(" — ")[0] for l in matches] == ["davisco", "sacco", "remoteco"]
    assert matches[0].startswith("- 📍 45")
    assert any("New Radar matches: 3 (2 local" in l for l in lines)
    assert any("Applied this week (all sources): 1 (1 came from the Radar)" in l for l in lines)


def test_notion_create_page_chunks_blocks(monkeypatch):
    from job_radar import notion_out
    calls = []

    def fake(method, url, **kw):
        calls.append((method, url, len(kw["json"].get("children", []))))
        return Resp(200, {"id": "pg1", "url": "https://notion/pg1"})
    monkeypatch.setattr(notion_out.requests, "request", fake)
    t = notion_out.NotionTracker("tok", "db")
    url = t.create_page("parent", "Weekly", ["## H"] + [f"- line {i}" for i in range(150)])
    assert url == "https://notion/pg1"
    assert calls[0][0] == "POST" and calls[0][2] == 90
    assert calls[1] == ("PATCH", "https://api.notion.com/v1/blocks/pg1/children", 61)


def test_local_share_keeps_remote_lanes_alive():
    loc = [P(f"L{i}", "Davis, CA") for i in range(10)]
    rem = [P(f"R{i}", "Remote") for i in range(10)]
    q = locality.local_first_with_share(rem + loc, cap=10, share=0.6)
    assert [p.title for p in q[:10]] == [f"L{i}" for i in range(6)] + [f"R{i}" for i in range(4)]
    assert [p.title for p in q[10:14]] == ["R4", "R5", "R6", "R7"] and len(q) == 20
    only_local = locality.local_first_with_share(loc, cap=10, share=0.6)
    assert [p.title for p in only_local] == [f"L{i}" for i in range(10)]
    few_remote = locality.local_first_with_share(loc + rem[:2], cap=10, share=0.6)
    assert [p.title for p in few_remote[:10]] == [f"L{i}" for i in range(8)] + ["R0", "R1"]
