"""Write matches into the Master Job Tracker — Live (Notion).

Property names match that database exactly. Rows are created with Status "Not Applied"
and a "RADAR:" prefix in Notes, so nothing is submitted anywhere — a person reviews first.
`create_database` builds a tracker with exactly these columns for a new user.
Dedupe key is the "External ID" column.
"""
from __future__ import annotations

import datetime as dt
import logging
import time

import requests

from .ingest import Posting
from .profile import PROFILE

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


# --- bootstrap: build a tracker database with every column the radar writes -------------------
STATUS_OPTIONS = ["Not Applied", "Drafting", "Applied", "Screening", "Interviewing", "Offer",
                  "Rejected", "Ghosted", "Withdrew"]
FIT_OPTIONS = ["🟢 Strong", "🟡 Stretch", "🎯 Reach", "⚪ Skip"]
SOURCE_OPTIONS = ["Greenhouse", "Lever", "Ashby", "Workday"]
COVER_OPTIONS = ["Not Started", "Drafted", "Sent"]


def _select(options: list[str]) -> dict:
    return {"select": {"options": [{"name": o} for o in options]}}


def tracker_schema() -> dict:
    """Notion property definitions, matching build_properties() and the kit/prep writers."""
    return {
        "Company": {"title": {}},
        "Role / Title": {"rich_text": {}},
        "JD URL": {"url": {}},
        "External ID": {"rich_text": {}},
        "Status": _select(STATUS_OPTIONS),
        "Fit": _select(FIT_OPTIONS),
        "Lane / Category": _select(list(PROFILE.lanes)),
        "Location": _select(PROFILE.locations),
        "Source Platform": _select(SOURCE_OPTIONS),
        "Resume Version": _select(list(PROFILE.resume_versions)),
        "Cover Letter Status": _select(COVER_OPTIONS),
        "Next Action": {"rich_text": {}},
        "Notes": {"rich_text": {}},
        "Date Found": {"created_time": {}},
    }


REQUIRED_PROPERTIES = {
    "Company": "title", "Role / Title": "rich_text", "JD URL": "url", "External ID": "rich_text",
    "Status": "select", "Fit": "select", "Lane / Category": "select", "Location": "select",
    "Source Platform": "select", "Resume Version": "select", "Cover Letter Status": "select",
    "Next Action": "rich_text", "Notes": "rich_text",
}


def page_id_from(ref: str) -> str:
    """Accept a Notion page URL or a bare id (with or without dashes)."""
    import re
    dashed = re.findall(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", ref)
    plain = re.findall(r"(?<![0-9a-zA-Z])[0-9a-fA-F]{32}(?![0-9a-zA-Z])", ref)
    m = [x.replace("-", "") for x in dashed] or plain
    if not m:
        raise SystemExit(f"Could not find a Notion page id in {ref!r}. Paste the page URL from Share → Copy link.")
    h = m[-1].lower()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def _headers(token: str) -> dict:
    if not token:
        raise SystemExit("NOTION_TOKEN is not set.")
    return {"Authorization": f"Bearer {token}", "Notion-Version": VERSION, "Content-Type": "application/json"}


def create_database(token: str, parent_page_id: str, title: str = "Job Radar Tracker") -> tuple[str, str]:
    """Create the tracker under a page the integration can see. Returns (database_id, url)."""
    r = _request("POST", f"{API}/databases", headers=_headers(token), json={
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": _rt(title),
        "properties": tracker_schema(),
    })
    data = r.json()
    return data["id"].replace("-", ""), data.get("url", "")


def missing_properties(props: dict) -> list[str]:
    """Names (with the expected type) that a database is missing or has with the wrong type."""
    out = []
    for name, typ in REQUIRED_PROPERTIES.items():
        have = (props.get(name) or {}).get("type")
        if have != typ:
            out.append(f"{name} ({typ})" + (f", found {have}" if have else ""))
    return out


def check_database(token: str, database_id: str) -> list[str]:
    data = _request("GET", f"{API}/databases/{database_id}", headers=_headers(token)).json()
    return missing_properties(data.get("properties", {}))
