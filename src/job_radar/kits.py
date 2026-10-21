"""Application kits for the top Radar matches (Batch API, 50% off).

For each selected tracker row: fetch the full job description from the ATS API, draft a
kit (tailored summary, resume bullets, cover note, "why this company" answer, likely
interview topics), then have a judge model check every claim against the resume.
Kits are appended to the row's Notion page and marked "verify before use".
Nothing is ever sent or submitted.
"""
from __future__ import annotations

import json
import logging
import re
import time

import requests

from . import score as sc
from .config import Settings, load_resume
from .cost import CostMeter
from .ingest import strip_html

log = logging.getLogger(__name__)

KIT_SYSTEM = """You prepare application materials for ONE candidate applying to ONE job.
The candidate's resume is below. It is the only source of facts about him.

Hard rules:
1. Use ONLY experience, employers, tools, projects, and numbers that appear in the resume.
   Reword and reorder freely; never invent metrics, titles, dates, tools, or outcomes.
2. Never state years of experience. Say "extensive experience" if you need to.
3. Plain, specific, confident. No clichés ("passionate", "fast-paced", "synergy"), no
   flattery of the company, no em-dash-heavy prose.
4. Where the job asks for something the resume does not show, do not paper over it;
   list it under gaps with an honest way to address it (adjacent experience or willingness
   to learn).

Write:
- summary: a 2-sentence resume summary tailored to this job.
- bullets: 5 resume bullets ordered by relevance to this job, each grounded in the resume.
- skills_to_feature: up to 8 skills from the resume to put first for this job.
- cover_note: 150-220 words. Why this role, two concrete matches from the resume, what he
  would do first. End with the line "Oscar Leung".
- why_company: 60-110 words answering "Why do you want to work here?", specific to the
  company and team in the posting.
- gaps: up to 3 requirements the resume does not clearly show, each with how to address it.
- interview_topics: 4 topics or questions he should prepare for, each tied to the posting.

Answer in the required JSON format."""

KIT_SCHEMA = {
    "name": "record_kit",
    "description": "Application kit for this posting.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "bullets": {"type": "array", "items": {"type": "string"}},
            "skills_to_feature": {"type": "array", "items": {"type": "string"}},
            "cover_note": {"type": "string"},
            "why_company": {"type": "string"},
            "gaps": {"type": "array", "items": {"type": "string"}},
            "interview_topics": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["summary", "bullets", "skills_to_feature", "cover_note", "why_company",
                     "gaps", "interview_topics"],
    },
}

JUDGE_SYSTEM = """You audit application materials for fabrication. The candidate's resume is
below and is the only source of truth. A claim is unsupported if any employer, title, tool,
metric, project, date, or outcome it states (or clearly implies) is not in the resume.
Rewording, reordering, and summarizing real facts is fine. Statements of interest or intent
("I'd start by...") are fine. Gaps sections are fine.
List every unsupported claim with a short quote and why. Answer in the required JSON format."""

JUDGE_SCHEMA = {
    "name": "record_audit",
    "description": "Fabrication audit of one application kit.",
    "input_schema": {
        "type": "object",
        "properties": {
            "unsupported": {"type": "array", "items": {"type": "object", "properties": {
                "quote": {"type": "string"}, "why": {"type": "string"}},
                "required": ["quote", "why"]}},
            "verdict": {"type": "string", "enum": ["clean", "minor", "major"]},
        },
        "required": ["unsupported", "verdict"],
    },
}


# --- job descriptions straight from the ATS (Ashby pages are JS-rendered, so scraping fails)
def fetch_jd(external_id: str, url: str = "", max_chars: int = 14000) -> str:
    try:
        ats, org, jid = external_id.split(":", 2)
    except ValueError:
        ats = org = jid = ""
    try:
        if ats == "gh":
            j = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{org}/jobs/{jid}",
                             timeout=30).json()
            return strip_html(j.get("content", ""))[:max_chars]
        if ats == "lv":
            j = requests.get(f"https://api.lever.co/v0/postings/{org}/{jid}", timeout=30).json()
            lists = "\n".join(f"{b.get('text', '')}\n{strip_html(b.get('content', ''))}"
                              for b in j.get("lists", []))
            return (j.get("descriptionPlain", "") + "\n" + lists)[:max_chars]
        if ats == "ab":
            data = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{org}",
                                timeout=30).json()
            for j in data.get("jobs", []):
                if j.get("id") == jid:
                    return (j.get("descriptionPlain", "")
                            or strip_html(j.get("descriptionHtml", "")))[:max_chars]
            return ""
        if url:
            r = requests.get(url, timeout=30, headers={"User-Agent": "job-radar/0.1"})
            text = re.sub(r"(?is)<(script|style).*?</\1>", "", r.text)
            return strip_html(text)[:max_chars]
    except (requests.RequestException, ValueError) as e:
        log.warning("JD fetch failed %s: %s", external_id or url, e)
    return ""


def radar_score(notes: str) -> int:
    m = re.match(r"\s*RADAR:\s*(\d+)/100", notes or "")
    return int(m.group(1)) if m else -1


def kit_markdown(kit: dict, audit: dict | None) -> list[str]:
    """Kit as plain paragraphs/bullets for Notion blocks (heading prefix '## ', bullet '- ')."""
    out = ["## Application kit (Radar) — verify before use"]
    if audit is None:
        out.append("Fabrication check: not run.")
    elif audit["verdict"] == "clean":
        out.append("Fabrication check: ✅ clean — every claim traced to the resume.")
    else:
        out.append(f"Fabrication check: ⚠️ {audit['verdict']} — fix these before using:")
        out += [f"- \"{u['quote']}\" — {u['why']}" for u in audit["unsupported"]]
    out.append("### Tailored summary")
    out.append(kit["summary"])
    out.append("### Resume bullets (put these first)")
    out += [f"- {b}" for b in kit["bullets"]]
    out.append("### Skills to feature")
    out.append(", ".join(kit["skills_to_feature"]))
    out.append("### Cover note")
    out += [p for p in kit["cover_note"].split("\n") if p.strip()]
    out.append("### Why this company")
    out.append(kit["why_company"])
    if kit["gaps"]:
        out.append("### Gaps and how to address them")
        out += [f"- {g}" for g in kit["gaps"]]
    out.append("### Prepare for")
    out += [f"- {t}" for t in kit["interview_topics"]]
    return out


def _run_batch(client, reqs: list[dict], label: str) -> dict:
    b = client.messages.batches.create(requests=reqs)
    print(f"{label} batch {b.id}: {len(reqs)} requests")
    while client.messages.batches.retrieve(b.id).processing_status != "ended":
        time.sleep(20)
    return {r.custom_id: r.result for r in client.messages.batches.results(b.id)}


REVISE_SYSTEM = """You fix application materials so every claim is supported by the resume below.
You get a kit (JSON) and a list of unsupported claims a reviewer found. Rewrite ONLY what is
needed: remove or reword each flagged claim so it states what the resume actually says, and
keep everything else as is. Do not add new claims. Answer in the required JSON format."""


def _max_tokens(effort: str | None, base: int) -> int:
    return {"high": 24000, "xhigh": 48000, "max": 64000}.get(effort or "", base)


def run_kits(settings: Settings, limit: int, model: str, judge_model: str,
             min_score: int, max_usd: float, write_notion: bool, effort: str | None = None,
             revise: bool = False, redo: bool = False, label: str = "") -> None:
    import anthropic
    from .notion_out import NotionTracker

    resume = load_resume(settings)
    tracker = NotionTracker(settings.notion_token, settings.notion_database_id)
    rows = [r for r in tracker.radar_rows() if radar_score(tracker.text(r, "Notes")) >= min_score
            and (redo or tracker.select(r, "Cover Letter Status") != "Drafted")]
    rows.sort(key=lambda r: -radar_score(tracker.text(r, "Notes")))
    rows = rows[:limit]
    log.info("kit candidates: %d (effort=%s revise=%s redo=%s)", len(rows), effort, revise, redo)

    def sys_block(text):
        return [{"type": "text", "text": f"{text}\n\n<resume>\n{resume}\n</resume>",
                 "cache_control": {"type": "ephemeral"}}]

    reqs, meta = [], {}
    for r in rows:
        ext, url = tracker.text(r, "External ID"), (r["properties"]["JD URL"].get("url") or "")
        jd = fetch_jd(ext, url)
        if len(jd) < 300:
            log.warning("skip %s: no usable JD", ext or url)
            continue
        cid = r["id"].replace("-", "")
        company, role = tracker.text(r, "Company"), tracker.text(r, "Role / Title")
        meta[cid] = (r["id"], company, role, jd)
        reqs.append({"custom_id": cid, "params": {
            "model": model, "max_tokens": _max_tokens(effort, 4000), "system": sys_block(KIT_SYSTEM),
            "output_config": sc.output_config(KIT_SCHEMA, effort),
            "messages": [{"role": "user", "content":
                          f"Company: {company}\nRole: {role}\n\n<posting>\n{jd}\n</posting>"}],
        }})
    if not reqs:
        print("No kit candidates.")
        return

    client = anthropic.Anthropic()
    meter = CostMeter(budget_usd=max_usd)

    def collect(results, model_, schema_name, sink):
        for cid, res in results.items():
            if res.type != "succeeded":
                log.warning("%s: %s", cid, res.type)
                continue
            meter.add(model_, res.message.usage, batch=True)
            try:
                sink[cid] = sc.parse_message(res.message, schema_name)
            except ValueError as e:
                log.warning("%s unparseable: %s", cid, e)

    def judge(kits_: dict) -> dict:
        out: dict = {}
        jreqs = [{"custom_id": cid, "params": {
            "model": judge_model, "max_tokens": _max_tokens(effort, 2500), "system": sys_block(JUDGE_SYSTEM),
            "output_config": sc.output_config(JUDGE_SCHEMA, effort),
            "messages": [{"role": "user", "content":
                          "Audit this kit:\n" + json.dumps(k, ensure_ascii=False, indent=1)}],
        }} for cid, k in kits_.items()]
        if jreqs:
            collect(_run_batch(client, jreqs, "judge"), judge_model, JUDGE_SCHEMA["name"], out)
        for a in out.values():
            a["verdict"] = str(a.get("verdict", "")).lower() or "minor"
            if a["verdict"] not in ("clean", "minor", "major"):
                a["verdict"] = "minor"
        return out

    kits: dict = {}
    collect(_run_batch(client, reqs, "kits"), model, KIT_SCHEMA["name"], kits)
    audits = judge(kits) if kits and not meter.over_budget else {}

    revised = 0
    if revise and not meter.over_budget:
        flagged = {cid: k for cid, k in kits.items()
                   if cid in audits and audits[cid]["verdict"] != "clean"}
        rreqs = [{"custom_id": cid, "params": {
            "model": model, "max_tokens": _max_tokens(effort, 4000), "system": sys_block(REVISE_SYSTEM),
            "output_config": sc.output_config(KIT_SCHEMA, effort),
            "messages": [{"role": "user", "content":
                          "Kit:\n" + json.dumps(k, ensure_ascii=False, indent=1)
                          + "\n\nUnsupported claims to fix:\n"
                          + json.dumps(audits[cid]["unsupported"], ensure_ascii=False, indent=1)}],
        }} for cid, k in flagged.items()]
        fixed: dict = {}
        if rreqs:
            collect(_run_batch(client, rreqs, "revise"), model, KIT_SCHEMA["name"], fixed)
        if fixed and not meter.over_budget:
            reaudit = judge(fixed)
            for cid, k in fixed.items():
                kits[cid], revised = k, revised + 1
                if cid in reaudit:
                    audits[cid] = reaudit[cid]

    out_dir = settings.reports_dir / "kits"
    out_dir.mkdir(parents=True, exist_ok=True)
    verdicts = {"clean": 0, "minor": 0, "major": 0, "unchecked": 0}
    for cid, kit in kits.items():
        page_id, company, role, _ = meta[cid]
        audit = audits.get(cid)
        verdicts[audit["verdict"] if audit else "unchecked"] += 1
        lines = kit_markdown(kit, audit)
        if label:
            lines[0] = lines[0].replace("Application kit (Radar)", f"Application kit {label}")
        safe = re.sub(r"[^\w-]+", "_", f"{company}_{role}")[:80]
        (out_dir / f"{safe}.md").write_text(f"# {company} — {role}\n\n" + "\n\n".join(lines) + "\n")
        if write_notion:
            tracker.append_kit(page_id, lines)
    meter.log(settings.costs_path, f"kits{label and '-' + label}")
    print(f"kits={len(kits)} revised={revised} verdicts={verdicts} cost=${meter.spent:.4f}")
