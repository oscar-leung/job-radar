"""Write matches into the Master Job Tracker — Live (Notion).

Property names match that database exactly. Rows are created with Status "Not Applied"
and a "RADAR:" prefix in Notes, so nothing is submitted anywhere — Oscar reviews first.
Dedupe key is the "External ID" column.
"""
from __future__ import annotations

import datetime as dt
import logging
import time

import requests

from .ingest import Posting

log = logging.getLogger(__name__)
API = "https://api.notion.com/v1"
VERSION = "2022-06-28"


def _request(method: str, url: str, **kw) -> requests.Response:
    """Notion allows ~3 requests/sec; back off on 429 and 5xx using Retry-After."""
    for attempt in range(6):
        r = requests.request(method, url, timeout=30, **kw)
        if r.status_code == 429 or r.status_code >= 500:
            wait = float(r.headers.get("Retry-After", 2 ** attempt))
            log.warning("Notion %s, retrying in %.1fs", r.status_code, wait)
            time.sleep(min(wait, 30))
            continue
        r.raise_for_status()
        return r
    r.raise_for_status()
    return r


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
        self._ids: set[str] | None = None

    def _load_ids(self) -> set[str]:
        """One paged pass over rows that have an External ID (instead of a query per posting)."""
        ids, body = set(), {"filter": {"property": "External ID", "rich_text": {"is_not_empty": True}},
                            "page_size": 100}
        while True:
            data = _request("POST", f"{API}/databases/{self.db}/query", headers=self.h, json=body).json()
            for row in data.get("results", []):
                rt = row["properties"].get("External ID", {}).get("rich_text", [])
                if rt:
                    ids.add("".join(t.get("plain_text", "") for t in rt))
            if not data.get("has_more"):
                return ids
            body["start_cursor"] = data["next_cursor"]

    def exists(self, external_id: str) -> bool:
        if self._ids is None:
            self._ids = self._load_ids()
        return external_id in self._ids

    def add(self, p: Posting, s: dict) -> str:
        run_date = dt.date.today().isoformat()
        r = _request("POST", f"{API}/pages", headers=self.h,
                     json={"parent": {"database_id": self.db},
                           "properties": build_properties(p, s, run_date),
                           "children": build_children(s)})
        if self._ids is not None:
            self._ids.add(p.external_id)
        return r.json().get("url", "")

    @staticmethod
    def text(row: dict, name: str) -> str:
        prop = row["properties"].get(name, {})
        items = prop.get("title") or prop.get("rich_text") or []
        return "".join(i.get("plain_text", "") for i in items)

    @staticmethod
    def select(row: dict, name: str) -> str:
        sel = (row["properties"].get(name) or {}).get("select") or {}
        return sel.get("name", "")

    def _query_all(self, flt: dict) -> list[dict]:
        rows, body = [], {"filter": flt, "page_size": 100}
        while True:
            data = _request("POST", f"{API}/databases/{self.db}/query", headers=self.h, json=body).json()
            rows.extend(data.get("results", []))
            if not data.get("has_more"):
                return rows
            body["start_cursor"] = data["next_cursor"]

    def radar_rows(self) -> list[dict]:
        """Not Applied rows the Radar filed (Notes start with 'RADAR:')."""
        return self._query_all({"and": [
            {"property": "Status", "select": {"equals": "Not Applied"}},
            {"property": "Notes", "rich_text": {"starts_with": "RADAR:"}},
        ]})

    def append_kit(self, page_id: str, lines: list[str]) -> None:
        blocks = []
        for ln in lines:
            if ln.startswith("### "):
                blocks.append({"object": "block", "type": "heading_3",
                               "heading_3": {"rich_text": _rt(ln[4:])}})
            elif ln.startswith("## "):
                blocks.append({"object": "block", "type": "heading_2",
                               "heading_2": {"rich_text": _rt(ln[3:])}})
            elif ln.startswith("- "):
                blocks.append({"object": "block", "type": "bulleted_list_item",
                               "bulleted_list_item": {"rich_text": _rt(ln[2:])}})
            else:
                blocks.append({"object": "block", "type": "paragraph",
                               "paragraph": {"rich_text": _rt(ln)}})
        for i in range(0, len(blocks), 90):
            _request("PATCH", f"{API}/blocks/{page_id}/children", headers=self.h,
                     json={"children": blocks[i:i + 90]})
        _request("PATCH", f"{API}/pages/{page_id}", headers=self.h,
                 json={"properties": {"Cover Letter Status": {"select": {"name": "Drafted"}},
                                      "Next Action": {"rich_text": _rt(
                                          "Review application kit below (check flagged claims), then apply")}}})

    def backlog(self, limit: int = 200) -> list[dict]:
        """Rows worth a cover-note draft: Not Applied/Drafting, Strong or Stretch, with a JD URL."""
        body = {
            "filter": {"and": [
                {"or": [{"property": "Status", "select": {"equals": "Not Applied"}},
                        {"property": "Status", "select": {"equals": "Drafting"}}]},
                {"or": [{"property": "Fit", "select": {"equals": "🟢 Strong"}},
                        {"property": "Fit", "select": {"equals": "🟡 Stretch"}}]},
                {"property": "JD URL", "url": {"is_not_empty": True}},
                {"property": "Cover Letter Status", "select": {"does_not_equal": "Drafted"}},
            ]},
            "page_size": 100,
        }
        rows, cursor = [], None
        while len(rows) < limit:
            if cursor:
                body["start_cursor"] = cursor
            data = _request("POST", f"{API}/databases/{self.db}/query", headers=self.h, json=body).json()
            rows.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return rows[:limit]

    def mark_cover_drafted(self, page_id: str, note_md: str) -> None:
        _request("PATCH", f"{API}/pages/{page_id}", headers=self.h,
                 json={"properties": {"Cover Letter Status": {"select": {"name": "Drafted"}}}})
        paras = [p for p in note_md.split("\n\n") if p.strip()][:20]
        children = [{"object": "block", "type": "heading_3",
                     "heading_3": {"rich_text": _rt("Radar sweep — cover note draft (review before sending)")}}]
        children += [{"object": "block", "type": "paragraph",
                      "paragraph": {"rich_text": _rt(t)}} for t in paras]
        _request("PATCH", f"{API}/blocks/{page_id}/children", headers=self.h,
                 json={"children": children})
