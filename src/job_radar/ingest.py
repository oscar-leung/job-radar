"""Pull postings from public ATS job-board APIs (no scraping, no auth)."""
from __future__ import annotations

import html
import logging
import re
from dataclasses import asdict, dataclass

import requests

log = logging.getLogger(__name__)
TIMEOUT = 30
UA = {"User-Agent": "job-radar/0.1 (personal job search tool)"}


@dataclass
class Posting:
    source: str          # Greenhouse | Lever | Ashby  (matches tracker "Source Platform")
    company: str
    external_id: str     # stable dedupe key, e.g. gh:anthropic:12345
    title: str
    location: str
    url: str
    description: str

    def to_dict(self) -> dict:
        return asdict(self)


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]+")


def strip_html(text: str) -> str:
    """Turn ATS HTML into readable plain text."""
    text = html.unescape(text or "")
    text = re.sub(r"<(br|/p|/li|/h\d)\s*/?>", "\n", text, flags=re.I)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    lines = [_WS.sub(" ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _get(url: str, **params) -> dict | list | None:
    try:
        r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        log.warning("fetch failed %s: %s", url, e)
        return None


def fetch_greenhouse(board: str) -> list[Posting]:
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs", content="true")
    out = []
    for j in (data or {}).get("jobs", []):
        out.append(Posting(
            source="Greenhouse", company=board,
            external_id=f"gh:{board}:{j['id']}",
            title=j.get("title", ""),
            location=(j.get("location") or {}).get("name", ""),
            url=j.get("absolute_url", ""),
            description=strip_html(j.get("content", "")),
        ))
    return out


def fetch_lever(company: str) -> list[Posting]:
    data = _get(f"https://api.lever.co/v0/postings/{company}", mode="json")
    out = []
    for j in data or []:
        lists = "\n".join(
            f"{blk.get('text', '')}\n{strip_html(blk.get('content', ''))}"
            for blk in j.get("lists", [])
        )
        out.append(Posting(
            source="Lever", company=company,
            external_id=f"lv:{company}:{j['id']}",
            title=j.get("text", ""),
            location=(j.get("categories") or {}).get("location", "") or "",
            url=j.get("hostedUrl", ""),
            description=(j.get("descriptionPlain", "") + "\n" + lists).strip(),
        ))
    return out


def fetch_ashby(org: str) -> list[Posting]:
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{org}")
    out = []
    for j in (data or {}).get("jobs", []):
        out.append(Posting(
            source="Ashby", company=org,
            external_id=f"ab:{org}:{j['id']}",
            title=j.get("title", ""),
            location=j.get("location", "") or "",
            url=j.get("jobUrl", "") or j.get("applyUrl", ""),
            description=j.get("descriptionPlain", "") or strip_html(j.get("descriptionHtml", "")),
        ))
    return out


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


def fetch_all(companies: dict[str, list[str]]) -> list[Posting]:
    postings: list[Posting] = []
    for ats, slugs in companies.items():
        fetch = FETCHERS.get(ats)
        if not fetch:
            log.warning("unknown ATS %r in companies.yaml", ats)
            continue
        for slug in slugs or []:
            got = fetch(slug)
            log.info("%s/%s: %d postings", ats, slug, len(got))
            postings.extend(got)
    return postings


def keyword_prefilter(postings: list[Posting], include: list[str], exclude: list[str]) -> list[Posting]:
    """Free, deterministic first pass on titles before any model call."""
    inc = re.compile("|".join(include), re.I) if include else None
    exc = re.compile("|".join(exclude), re.I) if exclude else None
    keep = []
    for p in postings:
        if inc and not inc.search(p.title):
            continue
        if exc and exc.search(p.title):
            continue
        keep.append(p)
    return keep
