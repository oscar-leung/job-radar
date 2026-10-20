"""CLI entry point.

  python -m job_radar run [--dry-run] [--limit N]   daily radar pass
  python -m job_radar check-boards                  verify company slugs (no API cost)
  python -m job_radar sweep [--write-notion]        Option D: batch cover notes for backlog
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging

import yaml

from .config import Settings, load_resume
from .cost import CostMeter
from .ingest import FETCHERS, fetch_all, keyword_prefilter
from . import score as sc

log = logging.getLogger("job_radar")

# Where Oscar will actually work: remote, Davis/Sacramento/Yolo commute, or Bay Area (hybrid).
IN_AREA = {"Remote-US", "Davis", "Sacramento", "Yolo", "Bay Area"}
# International remote is fine, but only for the core lanes he is targeting.
GLOBAL_OK_LANES = {"QA/SDET", "AI Eval"}


def in_area(score: dict) -> bool:
    loc = score.get("location")
    return loc in IN_AREA or (loc == "Remote-Global" and score.get("lane") in GLOBAL_OK_LANES)


def _companies(settings: Settings) -> dict:
    return yaml.safe_load(settings.companies_path.read_text())


def cmd_check_boards(settings: Settings) -> None:
    cfg = _companies(settings)
    for ats, fetch in FETCHERS.items():
        for slug in cfg.get(ats) or []:
            n = len(fetch(slug))
            print(f"{'OK ' if n else 'ERR'} {ats:<10} {slug:<20} {n} postings")


def _clean_env() -> None:
    """Pasted secrets often carry stray whitespace or newlines; strip them."""
    import os
    for k in ("ANTHROPIC_API_KEY", "NOTION_TOKEN"):
        if os.getenv(k):
            os.environ[k] = os.environ[k].strip()


def load_seen(path, days: int, today: dt.date | None = None) -> dict:
    """external_id -> ISO date last scored. Entries older than `days` expire so reposts get a fresh look."""
    today = today or dt.date.today()
    try:
        raw = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}
    cutoff = today - dt.timedelta(days=days)
    return {k: v for k, v in raw.items() if dt.date.fromisoformat(v) >= cutoff}


def save_seen(path, seen: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(seen, sort_keys=True))


def cmd_run(settings: Settings, dry_run: bool, limit: int | None) -> None:
    import anthropic
    _clean_env()
    from .notion_out import NotionTracker

    cfg = _companies(settings)
    resume = load_resume(settings)
    client = anthropic.Anthropic()
    meter = CostMeter(budget_usd=settings.max_run_usd)
    tracker = None if dry_run else NotionTracker(settings.notion_token, settings.notion_database_id)

    postings = fetch_all({k: cfg.get(k) for k in FETCHERS})
    pre = keyword_prefilter(postings, cfg.get("title_include", []), cfg.get("title_exclude", []))
    seen = load_seen(settings.seen_path, settings.seen_days)
    before = len(pre)
    pre = [p for p in pre if p.external_id not in seen]
    log.info("skipping %d postings already scored in the last %d days", before - len(pre), settings.seen_days)
    if tracker:
        pre = [p for p in pre if not tracker.exists(p.external_id)]
    today = dt.date.today().isoformat()
    cap = min(limit or settings.max_scored, settings.max_scored)
    log.info("fetched=%d prefiltered_new=%d cap=%d", len(postings), len(pre), cap)

    f_sys = sc.system_blocks(sc.load_prompt(settings.prompts_dir, "filter_rubric.md"), resume)
    s_sys = sc.system_blocks(sc.load_prompt(settings.prompts_dir, "scoring_rubric.md"), resume)

    results, scored, skipped = [], 0, 0
    for p in pre:
        if meter.over_budget:
            log.warning("budget $%.2f reached — stopping", settings.max_run_usd)
            break
        if scored >= cap:
            break
        try:
            rel, usages = sc.is_relevant(client, settings.model_filter, f_sys, p)
            for u in usages:
                meter.add(settings.model_filter, u)
            if not rel["relevant"]:
                seen[p.external_id] = today
                continue
            s, usages = sc.score(client, settings.model_score, s_sys, p)
            for u in usages:
                meter.add(settings.model_score, u)
        except (ValueError, KeyError, TypeError) as e:
            skipped += 1
            log.warning("skipped %s (%s): %s", p.external_id, p.title, e)
            continue
        scored += 1
        seen[p.external_id] = today
        row = {"posting": p.to_dict(), "score": s, "notion_url": None}
        if tracker and in_area(s) and s["fit_score"] >= settings.fit_threshold:
            row["notion_url"] = tracker.add(p, s)
        results.append(row)

    if not dry_run:
        save_seen(settings.seen_path, seen)
    _write_report(settings, results, meter, len(postings), len(pre))
    meter.log(settings.costs_path, "run-dry" if dry_run else "run")
    hits = sum(r["score"]["fit_score"] >= settings.fit_threshold and in_area(r["score"])
               for r in results)
    print(f"scored={scored} skipped={skipped} matches>={settings.fit_threshold}: {hits} cost=${meter.spent:.4f}")


def _write_report(settings, results, meter, fetched, prefiltered) -> None:
    settings.reports_dir.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    (settings.reports_dir / f"run_{stamp}.json").write_text(json.dumps(
        {"fetched": fetched, "prefiltered": prefiltered, "cost_usd": round(meter.spent, 4),
         "by_model": meter.by_model, "results": results}, indent=2, ensure_ascii=False))
    lines = [f"# Radar run {stamp}", "",
             f"Fetched {fetched} · new after title filter {prefiltered} · "
             f"scored {len(results)} · cost ${meter.spent:.4f}", "",
             "| Score | Fit | Company | Title | Lane |", "|---|---|---|---|---|"]
    for r in sorted(results, key=lambda r: -r["score"]["fit_score"]):
        s, p = r["score"], r["posting"]
        lines.append(f"| {s['fit_score']} | {s['fit']} | {p['company']} | "
                     f"[{p['title']}]({p['url']}) | {s['lane']} |")
    (settings.reports_dir / f"run_{stamp}.md").write_text("\n".join(lines) + "\n")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="job_radar")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true", help="score but don't write to Notion")
    r.add_argument("--limit", type=int, default=None)
    sub.add_parser("check-boards")
    sw = sub.add_parser("sweep")
    sw.add_argument("--write-notion", action="store_true")
    sw.add_argument("--limit", type=int, default=150)
    sw.add_argument("--model", default="claude-opus-5-5")
    a = ap.parse_args()
    settings = Settings()

    if a.cmd == "check-boards":
        cmd_check_boards(settings)
    elif a.cmd == "run":
        cmd_run(settings, a.dry_run, a.limit)
    elif a.cmd == "sweep":
        from .sweep import run_sweep
        run_sweep(settings, a.limit, a.model, a.write_notion)


if __name__ == "__main__":
    main()
