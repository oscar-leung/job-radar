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


WORKDAY_SEARCH = ["quality", "test", "QA", "SDET", "automation", "evaluation", "analyst",
                  "support engineer", "devops", "release engineer"]


def _wd_parts(spec: str) -> tuple[str, str, str]:
    """'tenant|wd5|Site' -> (tenant, wd5, Site)."""
    t, w, site = spec.split("|")
    return t, w, site


def fetch_workday(spec: str, terms: list[str] | None = None, per_term: int = 100) -> list[Posting]:
    """Workday's public job-search JSON. Titles and locations only; descriptions are fetched
    later for the few postings that pass the free filters (see hydrate_workday)."""
    t, w, site = _wd_parts(spec)
    base = f"https://{t}.{w}.myworkdayjobs.com/wday/cxs/{t}/{site}/jobs"
    seen: dict[str, Posting] = {}
    for term in terms or WORKDAY_SEARCH:
        for offset in range(0, per_term, 20):
            try:
                r = requests.post(base, json={"appliedFacets": {}, "limit": 20, "offset": offset,
                                              "searchText": term}, headers={**UA, "Accept": "application/json"},
                                  timeout=TIMEOUT)
                r.raise_for_status()
                jobs = r.json().get("jobPostings", [])
            except (requests.RequestException, ValueError) as e:
                log.warning("workday %s %r: %s", t, term, e)
                break
            for j in jobs:
                path = j.get("externalPath", "")
                if path and path not in seen:
                    seen[path] = Posting(
                        source="Workday", company=t, external_id=f"wd:{spec}:{path}",
                        title=j.get("title", ""), location=j.get("locationsText", "") or "",
                        url=f"https://{t}.{w}.myworkdayjobs.com/{site}{path}", description="")
            if len(jobs) < 20:
                break
    return list(seen.values())


def workday_description(external_id: str) -> str:
    _, spec, path = external_id.split(":", 2)
    t, w, site = _wd_parts(spec)
    try:
        r = requests.get(f"https://{t}.{w}.myworkdayjobs.com/wday/cxs/{t}/{site}{path}",
                         headers={**UA, "Accept": "application/json"}, timeout=TIMEOUT)
        r.raise_for_status()
        info = r.json().get("jobPostingInfo", {})
        return strip_html(info.get("jobDescription", ""))
    except (requests.RequestException, ValueError) as e:
        log.warning("workday detail %s: %s", external_id, e)
        return ""


def hydrate_workday(postings: list[Posting]) -> list[Posting]:
    """Fill in descriptions for Workday postings (one request each) after filtering."""
    from concurrent.futures import ThreadPoolExecutor
    todo = [p for p in postings if p.source == "Workday" and not p.description]
    with ThreadPoolExecutor(max_workers=8) as ex:
        for p, d in zip(todo, ex.map(lambda p: workday_description(p.external_id), todo)):
            p.description = d
    return [p for p in postings if p.source != "Workday" or p.description]


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
            "workday": fetch_workday}


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


def location_prefilter(postings: list[Posting], keep_patterns: list[str]) -> list[Posting]:
    """Free second pass: keep a posting if its location mentions remote/anywhere or a place
    Oscar would work, or is blank. Drops e.g. on-site Texas or London before any model call.
    The model's location field still gates what gets filed."""
    postings = [p for p in postings if not non_us_only(p.location)]
    if not keep_patterns:
        return postings
    rx = re.compile("|".join(keep_patterns), re.I)
    return [p for p in postings if not (p.location or "").strip() or rx.search(p.location)]


# Oscar can only work from the US (no work authorization elsewhere). A "Remote" posting that
# is limited to another country (e.g. "Remote Canada", "Canada; Toronto") is not remote for him.
_NON_US = re.compile(
    r"\b(canada|canadian|toronto|vancouver|montreal|calgary|ontario|british columbia|"
    r"united kingdom|\buk\b|london|ireland|dublin|germany|berlin|france|paris|netherlands|"
    r"amsterdam|spain|poland|india|bangalore|bengaluru|hyderabad|singapore|japan|tokyo|"
    r"australia|sydney|brazil|mexico|emea|apac|latam|europe)\b", re.I)
_US = re.compile(r"\b(us|u\.s\.|usa|united states|americas|anywhere|worldwide|global)\b|"
                 r",\s*(ca|ny|wa|tx|ma|co|il|ga|dc)\b|california|new york|seattle|"
                 r"san francisco|bay area", re.I)


def non_us_only(location: str | None) -> bool:
    """True when a location names only places outside the US (so Oscar can't take it)."""
    loc = (location or "").strip()
    return bool(loc) and bool(_NON_US.search(loc)) and not _US.search(loc)
