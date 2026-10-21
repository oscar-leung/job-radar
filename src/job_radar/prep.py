"""Interview prep packs + outreach drafts for the top Radar matches (Batch API, high effort).

Per match: what the role really needs, likely interview questions with STAR answers built
only from the resume, topics to brush up on, questions to ask, a first-90-days plan, and two
outreach drafts (LinkedIn note, short email to the hiring manager). A judge checks every
answer against the resume. Everything goes to the tracker row in Notion as a draft.
Nothing is sent.
"""
from __future__ import annotations

import json
import logging
import re

from . import score as sc
from .config import Settings, load_resume
from .cost import CostMeter
from .kits import JUDGE_SCHEMA, JUDGE_SYSTEM, REVISE_SYSTEM, _max_tokens, _run_batch, fetch_jd, radar_score

log = logging.getLogger(__name__)

PREP_DONE = "Prep pack ready"

PREP_SYSTEM = """You coach ONE candidate for interviews for ONE job. His resume is below and is
the only source of facts about him. Base company and team details only on the posting; do not
invent facts about the company.

Hard rules:
1. STAR answers use ONLY situations, employers, tools, numbers and outcomes in the resume.
   If the resume has no direct example, say so in the answer and use the closest real one.
2. Never state years of experience.
3. Plain, specific, spoken-English answers he could say out loud in about 90 seconds.

Write:
- role_decoded: 3-5 bullets on what this team actually needs, read between the lines.
- likely_questions: 10 questions this interviewer is likely to ask (mix of behavioral,
  technical, and role-specific), each with a STAR answer from the resume.
- topics_to_review: up to 8 technical topics to brush up on before the interview, each with
  why it matters for this posting.
- questions_to_ask: 5 sharp questions he should ask them.
- first_90_days: a short 30/60/90-day plan.
- linkedin_note: a LinkedIn connection note to someone on this team, under 280 characters,
  starting with "Hi [Name]," and mentioning one specific thing from the posting.
- hiring_manager_email: a 90-130 word email to the hiring manager with a subject line on the
  first line ("Subject: ..."), starting "Hi [Name]," and signed "Oscar Leung".

Answer in the required JSON format."""

PREP_SCHEMA = {
    "name": "record_prep",
    "description": "Interview prep pack for this posting.",
    "input_schema": {
        "type": "object",
        "properties": {
            "role_decoded": {"type": "array", "items": {"type": "string"}},
            "likely_questions": {"type": "array", "items": {"type": "object", "properties": {
                "question": {"type": "string"}, "answer": {"type": "string"}},
                "required": ["question", "answer"]}},
            "topics_to_review": {"type": "array", "items": {"type": "string"}},
            "questions_to_ask": {"type": "array", "items": {"type": "string"}},
            "first_90_days": {"type": "string"},
            "linkedin_note": {"type": "string"},
            "hiring_manager_email": {"type": "string"},
        },
        "required": ["role_decoded", "likely_questions", "topics_to_review", "questions_to_ask",
                     "first_90_days", "linkedin_note", "hiring_manager_email"],
    },
}


def prep_markdown(p: dict, audit: dict | None) -> list[str]:
    out = ["## Interview prep pack (Radar) — verify before use"]
    if audit is None:
        out.append("Fabrication check: not run.")
    elif audit["verdict"] == "clean":
        out.append("Fabrication check: ✅ clean — every answer traced to the resume.")
    else:
        out.append(f"Fabrication check: ⚠️ {audit['verdict']} — fix these before using:")
        out += [f"- \"{u['quote']}\" — {u['why']}" for u in audit["unsupported"]]
    out.append("### What the role really needs")
    out += [f"- {x}" for x in p["role_decoded"]]
    out.append("### Likely questions (with answers from your resume)")
    for i, qa in enumerate(p["likely_questions"], 1):
        out.append(f"- Q{i}. {qa['question']}")
        out += [para for para in qa["answer"].split("\n") if para.strip()]
    out.append("### Brush up on")
    out += [f"- {x}" for x in p["topics_to_review"]]
    out.append("### Questions to ask them")
    out += [f"- {x}" for x in p["questions_to_ask"]]
    out.append("### First 90 days")
    out += [para for para in p["first_90_days"].split("\n") if para.strip()]
    out.append("### Outreach drafts (not sent)")
    out.append("LinkedIn note: " + p["linkedin_note"])
    out += [para for para in p["hiring_manager_email"].split("\n") if para.strip()]
    return out


def run_prep(settings: Settings, limit: int, model: str, judge_model: str, min_score: int,
             effort: str | None, max_usd: float, write_notion: bool, revise: bool = True) -> None:
    import anthropic
    from .notion_out import NotionTracker

    resume = load_resume(settings)
    tracker = NotionTracker(settings.notion_token, settings.notion_database_id)
    rows = [r for r in tracker.radar_rows() if radar_score(tracker.text(r, "Notes")) >= min_score
            and not tracker.text(r, "Next Action").startswith(PREP_DONE)]
    rows.sort(key=lambda r: -radar_score(tracker.text(r, "Notes")))
    rows = [r for r in rows if not tracker.has_heading(r["id"], "Interview prep pack")][:limit]

    def sys_block(text):
        return [{"type": "text", "text": f"{text}\n\n<resume>\n{resume}\n</resume>",
                 "cache_control": {"type": "ephemeral"}}]

    reqs, meta = [], {}
    for r in rows:
        ext, url = tracker.text(r, "External ID"), (r["properties"]["JD URL"].get("url") or "")
        jd = fetch_jd(ext, url)
        if len(jd) < 300:
            continue
        cid = r["id"].replace("-", "")
        company, role = tracker.text(r, "Company"), tracker.text(r, "Role / Title")
        meta[cid] = (r["id"], company, role)
        reqs.append({"custom_id": cid, "params": {
            "model": model, "max_tokens": _max_tokens(effort, 8000), "system": sys_block(PREP_SYSTEM),
            "output_config": sc.output_config(PREP_SCHEMA, effort),
            "messages": [{"role": "user", "content":
                          f"Company: {company}\nRole: {role}\n\n<posting>\n{jd}\n</posting>"}],
        }})
    log.info("prep candidates: %d", len(reqs))
    if not reqs:
        print("No prep candidates.")
        return

    client = anthropic.Anthropic()
    meter = CostMeter(budget_usd=max_usd)
    packs: dict = {}
    for cid, res in _run_batch(client, reqs, "prep").items():
        if res.type != "succeeded":
            log.warning("%s: %s", cid, res.type)
            continue
        meter.add(model, res.message.usage, batch=True)
        try:
            packs[cid] = sc.parse_message(res.message, PREP_SCHEMA["name"])
        except ValueError as e:
            log.warning("%s unparseable: %s", cid, e)

    def judge(items: dict) -> dict:
        out: dict = {}
        jreqs = [{"custom_id": cid, "params": {
            "model": judge_model, "max_tokens": _max_tokens(effort, 3000), "system": sys_block(JUDGE_SYSTEM),
            "output_config": sc.output_config(JUDGE_SCHEMA, effort),
            "messages": [{"role": "user", "content":
                          "Audit these interview answers and outreach drafts:\n"
                          + json.dumps(p, ensure_ascii=False, indent=1)}],
        }} for cid, p in items.items()]
        if not jreqs:
            return out
        for cid, res in _run_batch(client, jreqs, "prep-judge").items():
            if res.type != "succeeded":
                continue
            meter.add(judge_model, res.message.usage, batch=True)
            try:
                a = sc.parse_message(res.message, JUDGE_SCHEMA["name"])
            except ValueError:
                continue
            a["verdict"] = str(a.get("verdict", "")).lower()
            if a["verdict"] not in ("clean", "minor", "major"):
                a["verdict"] = "minor"
            out[cid] = a
        return out

    audits = judge(packs) if packs and not meter.over_budget else {}
    if revise and not meter.over_budget:
        flagged = {cid: p for cid, p in packs.items() if cid in audits and audits[cid]["verdict"] != "clean"}
        rreqs = [{"custom_id": cid, "params": {
            "model": model, "max_tokens": _max_tokens(effort, 8000), "system": sys_block(REVISE_SYSTEM),
            "output_config": sc.output_config(PREP_SCHEMA, effort),
            "messages": [{"role": "user", "content":
                          "Prep pack:\n" + json.dumps(p, ensure_ascii=False, indent=1)
                          + "\n\nUnsupported claims to fix (in STAR answers, drop invented results "
                          "rather than replacing them with new ones):\n"
                          + json.dumps(audits[cid]["unsupported"], ensure_ascii=False, indent=1)}],
        }} for cid, p in flagged.items()]
        fixed: dict = {}
        if rreqs:
            for cid, res in _run_batch(client, rreqs, "prep-revise").items():
                if res.type != "succeeded":
                    continue
                meter.add(model, res.message.usage, batch=True)
                try:
                    fixed[cid] = sc.parse_message(res.message, PREP_SCHEMA["name"])
                except ValueError:
                    pass
        if fixed and not meter.over_budget:
            reaudit = judge(fixed)
            for cid, p in fixed.items():
                packs[cid] = p
                if cid in reaudit:
                    audits[cid] = reaudit[cid]

    out_dir = settings.reports_dir / "prep"
    out_dir.mkdir(parents=True, exist_ok=True)
    verdicts = {"clean": 0, "minor": 0, "major": 0, "unchecked": 0}
    for cid, p in packs.items():
        page_id, company, role = meta[cid]
        audit = audits.get(cid)
        verdicts[audit["verdict"] if audit else "unchecked"] += 1
        lines = prep_markdown(p, audit)
        safe = re.sub(r"[^\w-]+", "_", f"{company}_{role}")[:80]
        (out_dir / f"{safe}.md").write_text(f"# {company} — {role}\n\n" + "\n\n".join(lines) + "\n")
        if write_notion:
            tracker.append_blocks(page_id, lines)
            tracker.set_next_action(page_id, f"{PREP_DONE}: review kit + prep pack below, then apply")
    meter.log(settings.costs_path, "prep")
    print(f"prep={len(packs)} verdicts={verdicts} cost=${meter.spent:.4f}")
