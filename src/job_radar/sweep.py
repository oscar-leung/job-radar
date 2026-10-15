"""Option D — batch cover-note drafts for the tracker backlog (50% off via Batch API).

Reads Not Applied/Drafting rows (Strong or Stretch fit, JD URL present), fetches each JD,
submits one Message Batch, waits, and writes drafts to reports/sweep/. With --write-notion
it also appends the draft to each row and sets Cover Letter Status = Drafted.
Nothing is ever sent or submitted.
"""
from __future__ import annotations

import logging
import re
import time

import requests

from .config import Settings, load_resume
from .cost import CostMeter
from .ingest import strip_html

log = logging.getLogger(__name__)

SWEEP_SYSTEM = """You draft short cover notes for one candidate. Rules:
- 150–220 words, plain and specific, no clichés, no flattery of the company.
- Use ONLY experience that appears in the resume. Never invent metrics, titles, tools, or dates.
- Do not state years of experience; say "extensive experience" if needed.
- Structure: why this role → 2 concrete matches from the resume → one line on what he'd do first.
- End with a simple sign-off line: "Oscar Leung".
"""


def _prop_text(row: dict, name: str) -> str:
    prop = row["properties"].get(name, {})
    items = prop.get("title") or prop.get("rich_text") or []
    return "".join(i.get("plain_text", "") for i in items)


def _jd_text(url: str, max_chars: int = 12000) -> str:
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "job-radar/0.1"})
        r.raise_for_status()
        text = re.sub(r"(?is)<(script|style).*?</\1>", "", r.text)
        return strip_html(text)[:max_chars]
    except requests.RequestException as e:
        log.warning("JD fetch failed %s: %s", url, e)
        return ""


def run_sweep(settings: Settings, limit: int, model: str, write_notion: bool) -> None:
    import anthropic
    from .notion_out import NotionTracker

    resume = load_resume(settings)
    tracker = NotionTracker(settings.notion_token, settings.notion_database_id)
    rows = tracker.backlog(limit)
    log.info("backlog rows: %d", len(rows))

    system = [{"type": "text", "text": f"{SWEEP_SYSTEM}\n<resume>\n{resume}\n</resume>",
               "cache_control": {"type": "ephemeral"}}]
    requests_, meta = [], {}
    for row in rows:
        url = row["properties"]["JD URL"]["url"]
        jd = _jd_text(url)
        if len(jd) < 300:
            continue
        cid = row["id"].replace("-", "")
        meta[cid] = (row["id"], _prop_text(row, "Company"), _prop_text(row, "Role / Title"))
        requests_.append({"custom_id": cid, "params": {
            "model": model, "max_tokens": 700, "system": system,
            "messages": [{"role": "user", "content":
                          f"Company: {meta[cid][1]}\nRole: {meta[cid][2]}\n\n<jd>\n{jd}\n</jd>\n\n"
                          "Write the cover note."}],
        }})
    if not requests_:
        print("Nothing to sweep.")
        return

    client = anthropic.Anthropic()
    batch = client.messages.batches.create(requests=requests_)
    print(f"batch {batch.id}: {len(requests_)} requests submitted")
    while client.messages.batches.retrieve(batch.id).processing_status != "ended":
        time.sleep(30)

    meter = CostMeter(budget_usd=1e9)
    out_dir = settings.reports_dir / "sweep"
    out_dir.mkdir(parents=True, exist_ok=True)
    done = 0
    for res in client.messages.batches.results(batch.id):
        if res.result.type != "succeeded":
            log.warning("%s: %s", res.custom_id, res.result.type)
            continue
        msg = res.result.message
        meter.add(model, msg.usage, batch=True)
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        page_id, company, role = meta[res.custom_id]
        safe = re.sub(r"[^\w-]+", "_", f"{company}_{role}")[:80]
        (out_dir / f"{safe}.md").write_text(f"# {company} — {role}\n\n{text}\n")
        if write_notion:
            tracker.mark_cover_drafted(page_id, text)
        done += 1
    meter.log(settings.costs_path, "sweep")
    print(f"drafted {done} cover notes → {out_dir}  cost=${meter.spent:.4f}")
