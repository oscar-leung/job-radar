"""Feedback loop: Oscar's Status changes in the tracker become labels.

    python -m job_radar feedback            # pull labels from Notion -> data/feedback.jsonl
    python -m job_radar retune [--write]    # monthly: suggest (and optionally save) a new cutoff

Labels (from the "Status" column of Radar-filed rows):
  interview   Screening, Interview Scheduled/Done, Offer, Negotiating, Accepted   (strong yes)
  applied     Applied, Drafting, SOQ ready, Rejected, Ghosted, Withdrew           (yes: he chose it;
              a rejection is the employer's call, not a fit signal)
  skip        Skipped/Not Interested (any status starting with those words)       (no)
  skip_loc    "Skipped – commute" / "Skipped – onsite"                           (no, for location only)
  ignored     still "Not Applied" N+ days after filing                           (weak no)

How they are used, all without extra model calls:
  1. Few-shot calibration: a short list of recent decisions is added to the scorer's cached
     system prompt (titles only, ~300 tokens). Location-only skips are left out because the
     scorer is told to ignore location.
  2. Retune: Radar's own past scores (in Notes, "RADAR: 72/100") vs. these labels give a
     free threshold sweep, so the cutoff can be retuned monthly without re-scoring anything.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

POSITIVE_STRONG = {"screening", "interview scheduled", "interview done", "interview", "offer",
                   "negotiating", "accepted"}
POSITIVE = {"applied", "drafting", "rejected", "ghosted", "withdrew"}
LOCATION_WORDS = ("commute", "onsite", "on-site", "location", "relocat")


def label_for_status(status: str, days_since_filed: int | None = None,
                     ignored_after_days: int = 21) -> str | None:
    """Map a tracker Status to a feedback label (None = no signal yet)."""
    s = (status or "").strip().casefold()
    if not s:
        return None
    if s in POSITIVE_STRONG or s.startswith("interview"):
        return "interview"
    if s in POSITIVE or s.startswith("soq ready"):
        return "applied"
    if s.startswith("skipped") or s.startswith("not interested") or s.startswith("pass"):
        return "skip_loc" if any(w in s for w in LOCATION_WORDS) else "skip"
    if s == "not applied" and days_since_filed is not None and days_since_filed >= ignored_after_days:
        return "ignored"
    return None


def is_positive(label: str) -> bool:
    return label in ("interview", "applied")


@dataclass
class Feedback:
    external_id: str
    label: str
    status: str
    radar_score: int
    company: str
    title: str
    location: str
    lane: str
    filed: str          # ISO date the Radar filed it
    url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


_RADAR = re.compile(r"\s*RADAR:\s*(\d+)/100\s*\((\d{4}-\d{2}-\d{2})\)")


def parse_notes(notes: str) -> tuple[int, str]:
    """('RADAR: 72/100 (2026-10-20). ...') -> (72, '2026-10-20'); (-1, '') if not a Radar row."""
    m = _RADAR.match(notes or "")
    return (int(m.group(1)), m.group(2)) if m else (-1, "")


def from_rows(rows: list[dict], text, select, today: dt.date | None = None,
              ignored_after_days: int = 21) -> list[Feedback]:
    """Turn Notion rows into Feedback. `text`/`select` read properties (NotionTracker helpers)."""
    today = today or dt.date.today()
    out = []
    for r in rows:
        score, filed = parse_notes(text(r, "Notes"))
        if score < 0:
            continue
        days = (today - dt.date.fromisoformat(filed)).days if filed else None
        status = select(r, "Status")
        lab = label_for_status(status, days, ignored_after_days)
        if lab is None:
            continue
        url = ((r.get("properties", {}).get("JD URL") or {}).get("url")) or ""
        out.append(Feedback(external_id=text(r, "External ID"), label=lab, status=status,
                            radar_score=score, company=text(r, "Company"),
                            title=text(r, "Role / Title"), location=select(r, "Location"),
                            lane=select(r, "Lane / Category"), filed=filed, url=url))
    return out


def save(path: Path, items: list[Feedback]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(f.to_dict(), ensure_ascii=False) + "\n" for f in items))


def load(path: Path) -> list[Feedback]:
    if not path.exists():
        return []
    return [Feedback(**json.loads(l)) for l in path.read_text().splitlines() if l.strip()]


# --- 1. few-shot calibration block ---------------------------------------------------------
_LABEL_TEXT = {"interview": "INTERVIEWING", "applied": "APPLIED", "skip": "NOT INTERESTED",
               "ignored": "IGNORED (left unapplied 3+ weeks)"}


def few_shot_block(items: list[Feedback], n: int = 12) -> str:
    """Compact, recent-first examples: up to n/2 yes and n/2 no. Location-only skips are
    excluded (the scorer ignores location). Explicit skips are preferred over 'ignored'."""
    if n <= 0:
        return ""
    recent = sorted(items, key=lambda f: f.filed, reverse=True)
    yes = [f for f in recent if is_positive(f.label)]
    yes.sort(key=lambda f: f.label != "interview")          # interviews first, stable by recency
    no = [f for f in recent if f.label == "skip"] + [f for f in recent if f.label == "ignored"]
    yes_take = yes[:max(1, n // 2)]
    no_take = no[:n - len(yes_take)]
    yes_take = yes[:n - len(no_take)]          # if there are few "no"s, show more "yes"es
    picked = yes_take + no_take
    if not picked:
        return ""
    lines = ["## Oscar's recent decisions on past Radar matches (calibration only)",
             "Use these to calibrate what he actually acts on. Score the work in the new posting;",
             "do not reward or penalize a company just because it appears here.", ""]
    for f in picked:
        lines.append(f"- {_LABEL_TEXT[f.label]}: {f.title} @ {f.company} "
                     f"[{f.lane or '?'}] (Radar scored {f.radar_score})")
    return "\n".join(lines)


# --- 2. monthly retune -----------------------------------------------------------------------
def sweep(items: list[Feedback], lo: int = 30, hi: int = 95, step: int = 5,
          include_ignored: bool = True) -> list[dict]:
    """Threshold sweep on Radar's own scores. Only rows the Radar filed are labeled, so this
    can tell us whether to RAISE the cutoff (drop things he never acts on); it cannot see
    postings below the cutoff that was in force when they were scored."""
    rows = [f for f in items if f.label != "skip_loc" and (include_ignored or f.label != "ignored")]
    pos_total = sum(is_positive(f.label) for f in rows)
    out = []
    for th in range(lo, hi + 1, step):
        kept = [f for f in rows if f.radar_score >= th]
        pos = sum(is_positive(f.label) for f in kept)
        out.append({"threshold": th, "kept": len(kept), "positives_kept": pos,
                    "precision": pos / len(kept) if kept else 0.0,
                    "recall": pos / pos_total if pos_total else 0.0})
    return out


def recommend(items: list[Feedback], current: int, recall_floor: float = 0.9,
              min_labels: int = 20, max_step: int = 10) -> dict:
    """Pick the highest cutoff that still keeps >= recall_floor of what he acted on, moved at
    most max_step from the current cutoff per retune. Too little feedback -> keep current."""
    rows = [f for f in items if f.label != "skip_loc"]
    n_pos = sum(is_positive(f.label) for f in rows)
    res = {"current": current, "recommended": current, "n_labels": len(rows), "n_positive": n_pos,
           "reason": ""}
    if len(rows) < min_labels or n_pos < 3:
        res["reason"] = (f"only {len(rows)} labeled rows ({n_pos} positive); need {min_labels}+ "
                         "with 3+ positive — keeping the current cutoff")
        return res
    sw = sweep(rows, lo=max(0, current - max_step), hi=min(100, current + max_step), step=1)
    ok = [s for s in sw if s["recall"] >= recall_floor and s["kept"] > 0]
    best = max(ok, key=lambda s: s["threshold"]) if ok else None
    if best is None:
        res["reason"] = "no cutoff in range keeps enough of what you acted on — keeping current"
        return res
    base = next((s for s in sw if s["threshold"] == current), sw[0])
    res["precision_before"] = base["precision"]
    # Raise only when it actually filters out more of what he ignores. The sweep can't see
    # below the cutoff that filed these rows, so it never goes below current.
    if best["threshold"] <= current or best["precision"] <= base["precision"]:
        res["reason"] = (f"raising the cutoff would not improve the share acted on "
                         f"({base['precision']:.0%}) — keeping current")
        return res
    res["recommended"] = best["threshold"]
    res["precision_after"], res["recall_after"] = best["precision"], best["recall"]
    res["reason"] = (f"cutoff {res['recommended']} keeps {best['recall']:.0%} of applied/interview "
                     f"rows; share acted on goes {base['precision']:.0%} -> {best['precision']:.0%}")
    return res


def local_rates(items: list[Feedback]) -> dict:
    """Share of filed matches he acted on, local (Davis/Sacramento/Yolo) vs. not."""
    from .locality import is_local, tier_for
    out = {}
    for name, keep in (("local", True), ("non_local", False)):
        rows = [f for f in items if f.label != "skip_loc"
                and is_local(tier_for({"location": f.location}, f.location)) is keep]
        pos = sum(is_positive(f.label) for f in rows)
        out[name] = {"n": len(rows), "acted": pos, "rate": pos / len(rows) if rows else 0.0}
    return out


def retune_markdown(rec: dict, sw: list[dict], rates: dict, today: dt.date) -> str:
    lines = [f"# Radar retune — {today.isoformat()}", "",
             f"Cutoff: **{rec['current']} → {rec['recommended']}** ({rec['reason']})", "",
             f"Labels: {rec['n_labels']} rows, {rec['n_positive']} applied/interview.", "",
             "| Local? | Filed | Acted on | Rate |", "|---|---|---|---|"]
    for k, v in rates.items():
        lines.append(f"| {k.replace('_', '-')} | {v['n']} | {v['acted']} | {v['rate']:.0%} |")
    lines += ["", "| Cutoff | Kept | Acted on | Precision | Recall |", "|---|---|---|---|---|"]
    for s in sw:
        lines.append(f"| {s['threshold']} | {s['kept']} | {s['positives_kept']} | "
                     f"{s['precision']:.2f} | {s['recall']:.2f} |")
    lines += ["", "Caveat: only rows the Radar filed carry labels, so this can raise the cutoff "
              "but cannot justify lowering it. To test a lower cutoff, run one week with "
              "`fit_threshold` lowered by hand and retune again."]
    return "\n".join(lines) + "\n"


def load_tuning(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}
