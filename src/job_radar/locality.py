"""Local-first ranking. Oscar: "having local jobs is the best."

Every posting gets a location tier from its raw location text (before scoring, free) and
from the scorer's location field (after scoring). Tiers decide:
  1. scoring order — local postings are scored first, so the 40-posting daily cap is
     spent on Davis/Sacramento before remote;
  2. the filing cutoff — local matches get a lower fit cutoff (RADAR_LOCAL_BONUS);
  3. a "📍" tag in the tracker Notes and the order of the weekly summary.

The tier order is configurable via `location_priority` in config/companies.yaml.
"""
from __future__ import annotations

import re

# Default order (best first), per Oscar's request: Davis area, Sacramento area, hybrid, remote.
DEFAULT_PRIORITY = ["davis", "sacramento", "hybrid", "remote", "bay_area", "other"]

LABELS = {"davis": "Local: Davis/Yolo", "sacramento": "Local: Sacramento area",
          "hybrid": "Hybrid", "remote": "Remote", "bay_area": "Bay Area", "other": "Other"}

_DAVIS = re.compile(r"\b(davis|woodland|west sacramento|winters|dixon|esparto|knights landing|"
                    r"yolo)\b", re.I)
_SAC = re.compile(r"\b(sacramento|roseville|rocklin|folsom|rancho cordova|elk grove|citrus heights|"
                  r"natomas|carmichael|el dorado hills|granite bay|lincoln, ca|mcclellan|"
                  r"north highlands|fair oaks|orangevale|antelope|placer|galt)\b", re.I)
_HYBRID = re.compile(r"\bhybrid\b", re.I)
_REMOTE = re.compile(r"\b(remote|anywhere|distributed|work from home|wfh|telecommute)\b", re.I)
_BAY = re.compile(r"\b(san francisco|bay area|oakland|berkeley|emeryville|san jose|santa clara|"
                  r"sunnyvale|mountain view|palo alto|menlo park|redwood city|san mateo|foster city|"
                  r"fremont|newark|milpitas|cupertino|pleasanton|walnut creek|hayward|san ramon)\b", re.I)

# Scorer location enum -> tier
_SCORED = {"Davis": "davis", "Yolo": "davis", "Sacramento": "sacramento",
           "Remote-US": "remote", "Remote-Global": "remote", "Bay Area": "bay_area"}


def tier_from_text(location: str | None) -> str:
    """Tier from a posting's raw location text. 'West Sacramento' counts as Davis/Yolo.
    A posting that names a local city wins over 'remote' (e.g. 'Sacramento, CA or Remote')."""
    loc = location or ""
    if _DAVIS.search(loc):
        return "davis"
    if _SAC.search(loc):
        return "sacramento"
    if _HYBRID.search(loc):
        return "hybrid"
    if _REMOTE.search(loc):
        return "remote"
    if _BAY.search(loc):
        return "bay_area"
    return "other"


def tier_for(score: dict | None, raw_location: str | None = "", description: str = "") -> str:
    """Tier after scoring: the scorer's location field, refined by the raw text.
    Bay Area postings that say hybrid count as hybrid."""
    raw = tier_from_text(raw_location)
    if raw in ("davis", "sacramento"):
        return raw
    t = _SCORED.get((score or {}).get("location", ""), raw)
    if t in ("bay_area", "other") and (_HYBRID.search(raw_location or "")
                                       or _HYBRID.search((description or "")[:3000])):
        return "hybrid"
    return t


def rank(tier: str, priority: list[str] | None = None) -> int:
    order = priority or DEFAULT_PRIORITY
    return order.index(tier) if tier in order else len(order)


def is_local(tier: str) -> bool:
    return tier in ("davis", "sacramento")


def sort_local_first(postings: list, priority: list[str] | None = None) -> list:
    """Stable sort: Davis area, then Sacramento area, then hybrid, remote, ... (blank = other)."""
    return sorted(postings, key=lambda p: rank(tier_from_text(p.location), priority))


def local_first_with_share(postings: list, cap: int, share: float,
                           priority: list[str] | None = None) -> list:
    """Local-first order, but local postings take at most `share` of the first `cap` slots when
    there are non-local postings waiting — so a backlog of new local postings (e.g. the first
    run after adding EDJOIN) can't starve the remote QA/SDET lanes for days. Locals that don't
    fit today stay unscored, so they lead the queue again on the next run."""
    ordered = sort_local_first(postings, priority)
    local = [p for p in ordered if is_local(tier_from_text(p.location))]
    other = [p for p in ordered if not is_local(tier_from_text(p.location))]
    n_local = len(local) if not other else min(len(local), max(1, round(cap * share)))
    n_local = max(n_local, cap - len(other))           # fill with locals if non-locals run out
    return local[:n_local] + other + local[n_local:]


def cutoff_for(tier: str, fit_threshold: int, local_bonus: int) -> int:
    return fit_threshold - local_bonus if is_local(tier) else fit_threshold
