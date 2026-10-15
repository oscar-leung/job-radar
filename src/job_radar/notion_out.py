"""Write matches into the Master Job Tracker — Live (Notion).

Property names match that database exactly. Rows are created with Status "Not Applied"
and a "RADAR:" prefix in Notes, so nothing is submitted anywhere — Oscar reviews first.
Dedupe key is the "External ID" column.
"""
from __future__ import annotations

import datetime as dt
import logging

import requests

from .ingest import Posting

log = logging.getLogger(__name__)
API = "https://api.notion.com/v1"
VERSION = "2022-06-28"


def _rt(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": text[:2000]}}]


def build_properties(p: Posting, s: dict, run_date: str) -> dict:
    """Map a posting + score dict to Notion page properties."""
    notes = (
        f"RADAR: {s['fit_score']}/100 ({run_date}). "
        f"Why: {'; '.join(s.get('reasons', []))}. "
        f"Gaps: {'; '.join(s.get('gaps', [])) or 'none noted'}."
    )
    return {
        "Company": {"title": _rt(p.company)},
        "Role / Title": {"rich_text": _rt(p.title)},
        "JD URL": {"url": p.url or None},
        "External ID": {"rich_text": _rt(p.external_id)},
        "Status": {"select": {"name": "Not Applied"}},
        "Fit": {"select": {"name": s["fit"]}},
        "Lane / Category": {"select": {"name": s["lane"]}},
        "Location": {"select": {"name": s["location"]}},
        "Source Platform": {"select": {"name": p.source}},
        "Resume Version": {"select": {"name": s["resume_version"]}},
        "Next Action": {"rich_text": _rt("Review Radar match; tailor resume with bullets below")},
        "Notes": {"rich_text": _rt(notes)},
    }


def build_children(s: dict) -> list[dict]:
    blocks = [{
        "object": "block", "type": "heading_3",
        "heading_3": {"rich_text": _rt("Radar — tailored bullets (verify before use)")},
    }]
    for b in s.get("bullets", []):
        blocks.append({
            "object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": _rt(b)},
        })
    return blocks


class NotionTracker:
    def __init__(self, token: str, database_id: str):
        if not token:
            raise SystemExit("NOTION_TOKEN is not set.")
        self.db = database_id
        self.h = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": VERSION,
            "Content-Type": "application/json",
        }

    def exists(self, external_id: str) -> bool:
        r = requests.post(
            f"{API}/databases/{self.db}/query", headers=self.h, timeout=30,
            json={"filter": {"property": "External ID", "rich_text": {"equals": external_id}},
                  "page_size": 1},
        )
        r.raise_for_status()
        return bool(r.json().get("results"))

    def add(self, p: Posting, s: dict) -> str:
        run_date = dt.date.today().isoformat()
        r = requests.post(
            f"{API}/pages", headers=self.h, timeout=30,
            json={"parent": {"database_id": self.db},
                  "properties": build_properties(p, s, run_date),
                  "children": build_children(s)},
        )
        r.raise_for_status()
        return r.json().get("url", "")

    def backlog(self, limit: int = 200) -> list[dict]:
        """Rows worth a cover-note draft: Not Applied/Drafting, Strong or Stretch, with a JD URL."""
        body = {
            "filter": {"and": [
                {"or": [{"property": "Status", "select": {"equals": "Not Applied"}},
                        {"property": "Status", "select": {"equals": "Drafting"}}]},
                {"or": [{"property": "Fit", "select": {"equals": "🟢 Strong"}},
                        {"property": "Fit", "select": {"equals": "🟡 Stretch"}}]},
                {"property": "JD URL", "url": {"is_not_empty": True}},
            ]},
            "page_size": 100,
        }
        rows, cursor = [], None
        while len(rows) < limit:
            if cursor:
                body["start_cursor"] = cursor
            r = requests.post(f"{API}/databases/{self.db}/query", headers=self.h,
                              json=body, timeout=30)
            r.raise_for_status()
            data = r.json()
            rows.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return rows[:limit]

    def mark_cover_drafted(self, page_id: str, note_md: str) -> None:
        requests.patch(
            f"{API}/pages/{page_id}", headers=self.h, timeout=30,
            json={"properties": {"Cover Letter Status": {"select": {"name": "Drafted"}}}},
        ).raise_for_status()
        paras = [p for p in note_md.split("\n\n") if p.strip()][:20]
        children = [{"object": "block", "type": "heading_3",
                     "heading_3": {"rich_text": _rt("Radar sweep — cover note draft (review before sending)")}}]
        children += [{"object": "block", "type": "paragraph",
                      "paragraph": {"rich_text": _rt(t)}} for t in paras]
        requests.patch(
            f"{API}/blocks/{page_id}/children", headers=self.h, timeout=30,
            json={"children": children},
        ).raise_for_status()
