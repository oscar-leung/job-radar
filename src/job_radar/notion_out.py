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


def build_properties(p: Posting, s: dict, run_date: str, tier: str | None = None) -> dict:
    """Map a posting + score dict to Notion page properties. Local matches get a 📍 tag
    right after the score so they stand out in the tracker (Notes still start 'RADAR: N/100')."""
    from .locality import LABELS, is_local
    tag = f" 📍 {LABELS[tier]}." if tier and is_local(tier) else ""
    notes = (
        f"RADAR: {s['fit_score']}/100 ({run_date}).{tag} "
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


def lines_to_blocks(lines: list[str]) -> list[dict]:
    """Tiny markdown subset -> Notion blocks: '## ', '### ', '- ', plain paragraphs."""
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
    return blocks


def dedupe_key(external_id: str) -> tuple[str, str]:
    """(source prefix, posting id): 'gh:affirm:123' and 'gh:123' both map to ('gh', '123').
    Hand-logged 'ashby:<uuid>' is treated as the same source as 'ab:'."""
    parts = (external_id or "").split(":")
    src = {"ashby": "ab", "lever": "lv", "greenhouse": "gh"}.get(parts[0], parts[0])
    return src, parts[-1]


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
        if external_id in self._ids:
            return True
        # Rows edited by hand (e.g. when logging an application) may carry a short ID like
        # "gh:7727320003" instead of "gh:affirm:7727320003". Match on source + posting id too.
        key = dedupe_key(external_id)
        return any(dedupe_key(i) == key for i in self._ids)

    def add(self, p: Posting, s: dict, tier: str | None = None) -> str:
        run_date = dt.date.today().isoformat()
        r = _request("POST", f"{API}/pages", headers=self.h,
                     json={"parent": {"database_id": self.db},
                           "properties": build_properties(p, s, run_date, tier),
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

    def feedback_rows(self) -> list[dict]:
        """Every row the Radar filed (any Status) — the raw material for the feedback loop."""
        return self._query_all({"property": "Notes", "rich_text": {"starts_with": "RADAR:"}})

    def radar_rows_since(self, since: dt.date) -> list[dict]:
        """Rows the Radar filed on or after `since`."""
        return self._query_all({"and": [
            {"property": "Notes", "rich_text": {"starts_with": "RADAR:"}},
            {"timestamp": "created_time", "created_time": {"on_or_after": since.isoformat()}},
        ]})

    def applied_since(self, since: dt.date) -> list[dict]:
        """Rows from any source with Date Applied on or after `since`."""
        return self._query_all({"property": "Date Applied", "date": {"on_or_after": since.isoformat()}})

    def create_page(self, parent_page_id: str, title: str, lines: list[str], icon: str = "📡") -> str:
        """Create a child page under a regular Notion page (not the tracker database)."""
        blocks = lines_to_blocks(lines)
        r = _request("POST", f"{API}/pages", headers=self.h, json={
            "parent": {"page_id": parent_page_id},
            "icon": {"type": "emoji", "emoji": icon},
            "properties": {"title": {"title": _rt(title)}},
            "children": blocks[:90]})
        page = r.json()
        for i in range(90, len(blocks), 90):
            _request("PATCH", f"{API}/blocks/{page['id']}/children", headers=self.h,
                     json={"children": blocks[i:i + 90]})
        return page.get("url", "")

    def append_kit(self, page_id: str, lines: list[str]) -> None:
        self.append_blocks(page_id, lines)
        _request("PATCH", f"{API}/pages/{page_id}", headers=self.h,
                 json={"properties": {"Cover Letter Status": {"select": {"name": "Drafted"}},
                                      "Next Action": {"rich_text": _rt(
                                          "Review application kit below (check flagged claims), then apply")}}})

    def has_heading(self, page_id: str, prefix: str) -> bool:
        """True if the page body already has a heading starting with `prefix`."""
        cursor = None
        while True:
            url = f"{API}/blocks/{page_id}/children?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
            data = _request("GET", url, headers=self.h).json()
            for b in data.get("results", []):
                t = b.get("type", "")
                if t.startswith("heading_"):
                    txt = "".join(x.get("plain_text", "") for x in b[t].get("rich_text", []))
                    if txt.startswith(prefix):
                        return True
            if not data.get("has_more"):
                return False
            cursor = data["next_cursor"]

    def set_next_action(self, page_id: str, text: str) -> None:
        _request("PATCH", f"{API}/pages/{page_id}", headers=self.h,
                 json={"properties": {"Next Action": {"rich_text": _rt(text)}}})

    def append_blocks(self, page_id: str, lines: list[str]) -> None:
        blocks = lines_to_blocks(lines)
        for i in range(0, len(blocks), 90):
            _request("PATCH", f"{API}/blocks/{page_id}/children", headers=self.h,
                     json={"children": blocks[i:i + 90]})

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
