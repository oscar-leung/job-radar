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
    source: str          # Greenhouse | Lever | Ashby | Workday | ... (tracker "Source Platform")
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
    later for the few postings that pass the free filters (see hydrate).

    All Workday traffic goes through the shared "workday" limiter (few in flight, spaced
    requests) and retries 429/5xx with exponential backoff + jitter."""
    from . import http
    t, w, site = _wd_parts(spec)
    base = f"https://{t}.{w}.myworkdayjobs.com/wday/cxs/{t}/{site}/jobs"
    seen: dict[str, Posting] = {}
    for term in terms or WORKDAY_SEARCH:
        for offset in range(0, per_term, 20):
            try:
                r = http.request("POST", base, limiter="workday",
                                 json={"appliedFacets": {}, "limit": 20, "offset": offset,
                                       "searchText": term},
                                 headers={"Accept": "application/json"}, timeout=TIMEOUT)
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
    from . import http
    _, spec, path = external_id.split(":", 2)
    t, w, site = _wd_parts(spec)
    try:
        r = http.request("GET", f"https://{t}.{w}.myworkdayjobs.com/wday/cxs/{t}/{site}{path}",
                         limiter="workday", headers={"Accept": "application/json"}, timeout=TIMEOUT)
        info = r.json().get("jobPostingInfo", {})
        return strip_html(info.get("jobDescription", ""))
    except (requests.RequestException, ValueError) as e:
        log.warning("workday detail %s: %s", external_id, e)
        return ""


# --- SmartRecruiters (public Posting API: api.smartrecruiters.com/v1/companies/<id>/postings) ---
SR_API = "https://api.smartrecruiters.com/v1/companies"


def fetch_smartrecruiters(company: str, max_pages: int = 10) -> list[Posting]:
    """US postings only (country=us); descriptions are fetched later for survivors."""
    from . import http
    out = []
    for page in range(max_pages):
        try:
            data = http.request("GET", f"{SR_API}/{company}/postings", limiter="smartrecruiters",
                                params={"limit": 100, "offset": page * 100, "country": "us"}).json()
        except (requests.RequestException, ValueError) as e:
            log.warning("smartrecruiters %s: %s", company, e)
            break
        for j in data.get("content", []):
            loc = j.get("location") or {}
            where = ", ".join(x for x in (loc.get("city"), loc.get("region")) if x)
            if loc.get("remote"):
                where = f"Remote - US ({where})" if where else "Remote - US"
            out.append(Posting(
                source="SmartRecruiters", company=company,
                external_id=f"sr:{company}:{j['id']}", title=j.get("name", ""), location=where,
                url=f"https://jobs.smartrecruiters.com/{company}/{j['id']}", description=""))
        if len(data.get("content", [])) < 100:
            break
    return out


def smartrecruiters_description(external_id: str) -> str:
    from . import http
    _, company, jid = external_id.split(":", 2)
    try:
        j = http.request("GET", f"{SR_API}/{company}/postings/{jid}", limiter="smartrecruiters").json()
    except (requests.RequestException, ValueError) as e:
        log.warning("smartrecruiters detail %s: %s", external_id, e)
        return ""
    secs = ((j.get("jobAd") or {}).get("sections") or {})
    parts = [strip_html((secs.get(k) or {}).get("text", "")) for k in
             ("jobDescription", "qualifications", "additionalInformation")]
    return "\n\n".join(x for x in parts if x)


# --- EDJOIN (California K-12 jobs; the site's own public search JSON, robots.txt allows /) ---
EDJOIN = "https://www.edjoin.org"


def _edjoin_date(v) -> str:
    m = re.search(r"\d+", v or "")
    if not m:
        return ""
    import datetime as dt
    return dt.datetime.fromtimestamp(int(m.group()) / 1000, dt.timezone.utc).date().isoformat()


def fetch_edjoin(regions: str = "58,34", max_pages: int = 20) -> list[Posting]:
    """`regions` = EDJOIN county IDs, comma-separated (58 Yolo, 34 Sacramento, 31 Placer).
    Pulls every posting in those counties; the free title filter keeps certificated subs and
    IT/data roles and drops para/aide roles (see companies.yaml)."""
    from . import http
    out = []
    for page in range(1, max_pages + 1):
        params = {"rows": 100, "page": page, "sort": "postingDate", "order": "desc",
                  "keywords": "", "location": "", "searchType": "all", "regions": regions,
                  "jobTypes": "", "days": 0, "empType": "", "catID": 0, "onlineApps": "",
                  "recruitmentCenterID": 0, "stateID": 24, "regionID": 0, "districtID": 0,
                  "searchID": 0}
        try:
            data = http.request("GET", f"{EDJOIN}/Home/LoadJobs", limiter="edjoin", params=params).json()
        except (requests.RequestException, ValueError) as e:
            log.warning("edjoin page %d: %s", page, e)
            break
        for j in data.get("data", []):
            pid = j.get("postingID")
            city = (j.get("city") or "").strip()
            county = (j.get("countyName") or "").strip()
            loc = ", ".join(x for x in (city, "CA") if x) + (f" ({county} County)" if county else "")
            pay = " ".join(x for x in (j.get("PayRangeFrom"), j.get("PayRangeTo") and "- " + j["PayRangeTo"],
                                       j.get("PayRangeDropdown"), j.get("SingleRate"),
                                       j.get("SingleRateDropdown")) if x)
            summary = (f"District: {j.get('districtName', '')}\nJob type: {j.get('jobType', '')}\n"
                       f"Schedule: {j.get('FullTimePartTime', '')}\nPay: {pay}\n"
                       f"Posted: {_edjoin_date(j.get('postingDate'))}")
            out.append(Posting(
                source="EDJOIN", company=(j.get("districtName") or "").strip(),
                external_id=f"ed:edjoin:{pid}", title=(j.get("positionTitle") or "").strip(),
                location=loc, url=f"{EDJOIN}/Home/JobPosting/{pid}", description="",
            ))
            out[-1]._summary = summary  # type: ignore[attr-defined]
        if page >= int(data.get("totalPages") or 0):
            break
    return out


_EDJOIN_START = ("Salary Range", "Employment Type", "About the Employer", "Job Description")


def edjoin_description(external_id: str) -> str:
    """The posting page is public HTML (closed postings answer 410 but still render)."""
    from . import http
    pid = external_id.rsplit(":", 1)[-1]
    try:
        with http.LIMITERS["edjoin"]:
            r = requests.get(f"{EDJOIN}/Home/JobPosting/{pid}", headers=UA, timeout=TIMEOUT)
        if r.status_code not in (200, 410):
            r.raise_for_status()
    except requests.RequestException as e:
        log.warning("edjoin detail %s: %s", external_id, e)
        return ""
    body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", r.text)
    text = strip_html(body[body.find("<body"):] if "<body" in body else body)
    starts = [i for i in (text.find(k) for k in _EDJOIN_START) if i >= 0]
    return text[min(starts) if starts else 0:][:8000]


# --- iCIMS (sitemap.xml is published in each board's robots.txt; detail pages carry JSON-LD) ---
def fetch_icims(host: str, max_postings: int = 10000) -> list[Posting]:
    """`host` like 'careers-commonspirit'. Titles come from the sitemap URL slug; location and
    description come from the job page's JobPosting JSON-LD for the few that pass the title
    filter (see hydrate). The location filter is re-applied after hydrating."""
    from . import http
    base = f"https://{host}.icims.com"
    try:
        xml = http.request("GET", f"{base}/sitemap.xml", limiter="icims").text
    except requests.RequestException as e:
        log.warning("icims %s: %s", host, e)
        return []
    out = []
    for m in re.finditer(r"<loc>(https://[^<]+/jobs/(\d+)/([^/<]+)/job)</loc>", xml):
        url, jid, slug = m.groups()
        title = re.sub(r"-+", " ", slug).strip().title()
        out.append(Posting(source="iCIMS", company=host.removeprefix("careers-"),
                           external_id=f"ic:{host}:{jid}", title=title, location="",
                           url=url, description=""))
        if len(out) >= max_postings:
            break
    return out


def icims_detail(external_id: str, url: str) -> tuple[str, str, str]:
    """(title, location, description) from the posting's JSON-LD; blanks on failure."""
    from . import http
    import json as _json
    try:
        page = http.request("GET", url + ("&" if "?" in url else "?") + "in_iframe=1",
                            limiter="icims").text
    except requests.RequestException as e:
        log.warning("icims detail %s: %s", external_id, e)
        return "", "", ""
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', page, re.S):
        try:
            d = _json.loads(m.group(1))
        except ValueError:
            continue
        if d.get("@type") != "JobPosting":
            continue
        places = d.get("jobLocation") or []
        places = places if isinstance(places, list) else [places]
        locs = []
        for pl in places:
            a = (pl or {}).get("address") or {}
            bits = [a.get("addressLocality"), a.get("addressRegion")]
            bits = [b for b in bits if b and b != "UNAVAILABLE"]
            if bits:
                locs.append(", ".join(bits))
        if d.get("jobLocationType") == "TELECOMMUTE":
            locs.append("Remote")
        return d.get("title", ""), "; ".join(locs), strip_html(d.get("description", ""))
    return "", "", ""


# --- University of California systemwide job board (UC Davis, UC Davis Health, UCOP) ---
UC_BOARD = "https://jobs.universityofcalifornia.edu/site/advancedsearch"
UC_SEARCH = ["analyst", "quality", "test", "automation", "programmer", "developer",
             "information technology", "data", "systems", "support"]
UC_SOURCES = {"ucdavis": "UC Davis Campus", "ucdmed": "UC Davis Health", "ucop": "UCOP"}
_UC_SPOT = re.compile(r'<div class="jobspot">(.*?)(?=<div class="jobspot">|</main>|$)', re.S)


def _uc_field(block: str, cls: str) -> str:
    m = re.search(rf'class="{cls}[^"]*"[^>]*>(.*?)</', block, re.S)
    return html.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else ""


def parse_uc_results(page: str) -> list[Posting]:
    out = []
    for block in _UC_SPOT.findall(page):
        m = re.search(r'<a href="([^"]+)" class="jtitle"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        url, title = html.unescape(m.group(1)), html.unescape(re.sub(r"\s+", " ", m.group(2))).strip()
        jid = re.search(r"JobOpeningId=(\d+)", url)
        site = re.search(r"/psc/([a-z]+)/", url)
        if not jid:
            continue
        site_id = site.group(1) if site else "uc"
        campus = _uc_field(block, "jloc")
        where = _uc_field(block, "job_work_location").removeprefix("Work Location:").strip() or campus
        desc = "\n".join(x for x in (
            f"Campus: {campus}", _uc_field(block, "jtype"), _uc_field(block, "jfamily"),
            _uc_field(block, "jclose"), _uc_field(block, "jdesc")) if x)
        out.append(Posting(source=UC_SOURCES.get(site_id, "Direct"), company=f"UC {campus}".strip(),
                           external_id=f"uc:{site_id}:{jid.group(1)}", title=title,
                           location=f"{where}, CA" if where and ", CA" not in where else where,
                           url=url, description=desc))
    return out


def fetch_uc(campuses: str = "DV,DVMC", terms: list[str] | None = None, max_pages: int = 5) -> list[Posting]:
    """`campuses` = UC board campus codes (DV = UC Davis, DVMC = UC Davis Health, OP = UCOP).
    Public search pages (no robots.txt restrictions); results carry a description snippet,
    which is what gets scored (the full JD lives behind UC's PeopleSoft portal)."""
    from . import http
    seen: dict[str, Posting] = {}
    codes = [c.strip() for c in campuses.split(",") if c.strip()]
    for term in terms or UC_SEARCH:
        for page in range(1, max_pages + 1):
            params = [("MCampus[]", c) for c in codes] + [("keywords", term), ("search", "1"),
                                                           ("page", str(page))]
            try:
                text = http.request("GET", UC_BOARD, limiter="uc", params=params).text
            except requests.RequestException as e:
                log.warning("uc %s %r: %s", campuses, term, e)
                break
            got = parse_uc_results(text)
            for p in got:
                seen.setdefault(p.external_id, p)
            if len(got) < 10:
                break
    return list(seen.values())


def _detail(p: Posting) -> Posting | None:
    """Fill in the description (and, for iCIMS, title/location) for one posting."""
    if p.source == "Workday":
        p.description = workday_description(p.external_id)
    elif p.source == "SmartRecruiters":
        p.description = smartrecruiters_description(p.external_id)
    elif p.source == "EDJOIN":
        body = edjoin_description(p.external_id)
        p.description = (getattr(p, "_summary", "") + "\n\n" + body).strip()
    elif p.source == "iCIMS":
        title, loc, desc = icims_detail(p.external_id, p.url)
        p.title, p.location, p.description = title or p.title, loc, desc
    return p if p.description else None


NEEDS_DETAIL = {"Workday", "SmartRecruiters", "EDJOIN", "iCIMS"}


def hydrate(postings: list[Posting], max_per_source: dict[str, int] | None = None,
            location_patterns: list[str] | None = None,
            rejected: list[Posting] | None = None) -> list[Posting]:
    """Fetch descriptions only for postings that passed the free filters (one request each).
    `max_per_source` caps detail requests per run (e.g. iCIMS boards are national and big);
    the dict is decremented in place so the cap holds across several calls in one run.
    iCIMS locations are only known after hydrating, so the location filter runs again here;
    postings it drops are appended to `rejected` (so the caller can skip them next time)."""
    from concurrent.futures import ThreadPoolExecutor
    caps = max_per_source if max_per_source is not None else {}
    todo, keep = [], []
    for p in postings:
        if p.source not in NEEDS_DETAIL or p.description:
            keep.append(p)
            continue
        if p.source in caps:
            if caps[p.source] <= 0:
                continue
            caps[p.source] -= 1
        todo.append(p)
    with ThreadPoolExecutor(max_workers=8) as ex:   # per-host limiters cap real concurrency
        done = {id(p) for p in ex.map(_detail, todo) if p is not None}
    filled = [p for p in todo if id(p) in done]
    icims = [p for p in filled if p.source == "iCIMS"]
    if icims and location_patterns is not None:
        ok = {id(p) for p in location_prefilter(icims, location_patterns) if p.location}
        if rejected is not None:
            rejected.extend(p for p in icims if id(p) not in ok)
        filled = [p for p in filled if p.source != "iCIMS" or id(p) in ok]
    order = {id(p): i for i, p in enumerate(postings)}
    return sorted(keep + filled, key=lambda p: order[id(p)])


def hydrate_workday(postings: list[Posting]) -> list[Posting]:
    """Back-compat name (Workday was the first source that needed a detail fetch)."""
    return hydrate(postings)


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
            "workday": fetch_workday, "smartrecruiters": fetch_smartrecruiters,
            "edjoin": fetch_edjoin, "icims": fetch_icims, "uc": fetch_uc}


def fetch_all(companies: dict[str, list[str]], workday_workers: int | None = None) -> list[Posting]:
    """Fetch every board in parallel. Workday tenants run in their own small pool (they share
    one rate limit), everything else in a wider pool."""
    import os
    from concurrent.futures import ThreadPoolExecutor
    jobs = []
    for ats, slugs in companies.items():
        fetch = FETCHERS.get(ats)
        if not fetch:
            log.warning("unknown ATS %r in companies.yaml", ats)
            continue
        jobs += [(ats, fetch, str(slug)) for slug in slugs or []]

    def one(job):
        ats, fetch, slug = job
        try:
            got = fetch(slug)
        except Exception as e:  # one broken board must not sink the run
            log.warning("%s/%s failed: %s", ats, slug, e)
            return []
        log.info("%s/%s: %d postings", ats, slug, len(got))
        return got

    wd_workers = workday_workers or int(os.getenv("RADAR_WORKDAY_WORKERS") or 3)
    postings: list[Posting] = []
    with ThreadPoolExecutor(max_workers=12) as ex, ThreadPoolExecutor(max_workers=wd_workers) as wd:
        futs = [(wd if j[0] == "workday" else ex).submit(one, j) for j in jobs]
        for f in futs:
            postings.extend(f.result())
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
