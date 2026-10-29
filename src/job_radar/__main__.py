"""CLI entry point.

  python -m job_radar run [--dry-run] [--limit N]   daily radar pass
  python -m job_radar check-boards                  verify company slugs (no API cost)
  python -m job_radar sweep [--write-notion]        Option D: batch cover notes for backlog
  python -m job_radar kits [--limit 40] [--write-notion]  application kits for top Radar matches
  python -m job_radar feedback                      pull Status labels from Notion (free)
  python -m job_radar retune [--write]              monthly cutoff retune from feedback (free)
  python -m job_radar weekly [--write-notion]       weekly summary page in Notion (free)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging

import yaml
from concurrent.futures import ThreadPoolExecutor

from .config import Settings, load_resume
from .cost import CostMeter
from .ingest import FETCHERS, fetch_all, hydrate, keyword_prefilter, location_prefilter
from . import locality
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
            n = len(fetch(str(slug)))
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


def load_feedback_examples(settings: Settings, tracker) -> str:
    """Pull Oscar's Status decisions on past Radar rows and turn them into a few-shot block.
    One paged Notion query, no model calls. Any failure just means no examples."""
    from . import feedback as fb
    if settings.few_shot <= 0 or tracker is None:
        return ""
    try:
        items = fb.from_rows(tracker.feedback_rows(), tracker.text, tracker.select)
        fb.save(settings.feedback_path, items)
    except Exception as e:  # feedback is a nice-to-have; never block the daily run
        log.warning("feedback unavailable: %s", e)
        return ""
    block = fb.few_shot_block(items, settings.few_shot)
    log.info("feedback: %d labeled rows, %d few-shot examples", len(items),
             max(0, block.count("\n- ")))
    return block


def select_queue(pre: list, cap: int, settings: Settings, cfg: dict, seen: dict, today: str) -> list:
    """Local-first order, then fetch detail pages only until `cap` postings are ready to score
    (instead of hydrating everything that passed the free filters)."""
    priority = cfg.get("location_priority")
    pre = locality.local_first_with_share(pre, cap, settings.local_share, priority)
    caps = {"iCIMS": settings.icims_max_detail}
    queue, rejected, i = [], [], 0
    while len(queue) < cap and i < len(pre):
        batch = pre[i:i + (cap - len(queue)) + 5]
        i += len(batch)
        queue += hydrate(batch, caps, cfg.get("location_include", []), rejected)
    for p in rejected:          # wrong location once hydrated: don't fetch it again for a while
        seen[p.external_id] = today
    return queue[:cap]


def cmd_run(settings: Settings, dry_run: bool, limit: int | None) -> None:
    import anthropic
    _clean_env()
    from .notion_out import NotionTracker

    cfg = _companies(settings)
    resume = load_resume(settings)
    client = anthropic.Anthropic()
    meter = CostMeter(budget_usd=settings.max_run_usd)
    tracker = None if dry_run else NotionTracker(settings.notion_token, settings.notion_database_id)
    reader = tracker or (NotionTracker(settings.notion_token, settings.notion_database_id)
                         if settings.notion_token else None)

    postings = fetch_all({k: cfg.get(k) for k in FETCHERS})
    pre = keyword_prefilter(postings, cfg.get("title_include", []), cfg.get("title_exclude", []))
    pre = location_prefilter(pre, cfg.get("location_include", []))
    seen = load_seen(settings.seen_path, settings.seen_days)
    before = len(pre)
    pre = [p for p in pre if p.external_id not in seen]
    log.info("skipping %d postings already scored in the last %d days", before - len(pre), settings.seen_days)
    if tracker:
        pre = [p for p in pre if not tracker.exists(p.external_id)]
    today = dt.date.today().isoformat()
    cap = min(limit or settings.max_scored, settings.max_scored)
    n_new = len(pre)
    queue = select_queue(pre, cap, settings, cfg, seen, today)
    n_local = sum(locality.is_local(locality.tier_from_text(p.location)) for p in queue)
    log.info("fetched=%d prefiltered_new=%d cap=%d queued=%d (local %d)",
             len(postings), n_new, cap, len(queue), n_local)

    examples = load_feedback_examples(settings, reader)
    f_sys = sc.system_blocks(sc.load_prompt(settings.prompts_dir, "filter_rubric.md"), resume)
    s_sys = sc.system_blocks(sc.load_prompt(settings.prompts_dir, "scoring_rubric.md"), resume, examples)

    def assess(p):
        """Filter then score one posting. Returns (posting, score|None, usages, error)."""
        usages = []
        try:
            rel, u = sc.is_relevant(client, settings.model_filter, f_sys, p)
            usages += [(settings.model_filter, x) for x in u]
            if not rel["relevant"]:
                return p, None, usages, None
            s, u = sc.score(client, settings.model_score, s_sys, p)
            usages += [(settings.model_score, x) for x in u]
            return p, s, usages, None
        except (ValueError, KeyError, TypeError) as e:
            return p, None, usages, e

    results, scored, skipped = [], 0, 0
    workers = settings.workers
    for i in range(0, len(queue), workers * 4):
        if meter.over_budget:
            log.warning("budget $%.2f reached — stopping", settings.max_run_usd)
            break
        chunk = queue[i:i + workers * 4]
        with ThreadPoolExecutor(max_workers=workers) as ex:
            outcomes = list(ex.map(assess, chunk))
        for p, s, usages, err in outcomes:
            for model, u in usages:
                meter.add(model, u)
            if err is not None:
                skipped += 1
                log.warning("skipped %s (%s): %s", p.external_id, p.title, err)
                continue
            seen[p.external_id] = today
            if s is None:
                continue
            scored += 1
            tier = locality.tier_for(s, p.location, p.description)
            cutoff = locality.cutoff_for(tier, settings.fit_threshold, settings.local_bonus)
            row = {"posting": p.to_dict(), "score": s, "tier": tier, "cutoff": cutoff, "notion_url": None}
            if tracker and in_area(s) and s["fit_score"] >= cutoff:
                row["notion_url"] = tracker.add(p, s, tier)
            results.append(row)
        if not dry_run:  # save progress so a timeout or crash doesn't re-bill the same postings
            save_seen(settings.seen_path, seen)
        log.info("progress %d/%d scored=%d cost=$%.2f", min(i + len(chunk), len(queue)),
                 len(queue), scored, meter.spent)

    if not dry_run:
        save_seen(settings.seen_path, seen)
    _write_report(settings, results, meter, len(postings), n_new)
    meter.log(settings.costs_path, "run-dry" if dry_run else "run")
    hits = sum(r["score"]["fit_score"] >= r["cutoff"] and in_area(r["score"]) for r in results)
    local_hits = sum(r["score"]["fit_score"] >= r["cutoff"] and in_area(r["score"])
                     and locality.is_local(r["tier"]) for r in results)
    print(f"scored={scored} skipped={skipped} matches={hits} (local {local_hits}; cutoff "
          f"{settings.fit_threshold}, local {settings.fit_threshold - settings.local_bonus}) "
          f"cost=${meter.spent:.4f}")


def cmd_feedback(settings: Settings) -> None:
    from . import feedback as fb
    from .notion_out import NotionTracker
    _clean_env()
    t = NotionTracker(settings.notion_token, settings.notion_database_id)
    items = fb.from_rows(t.feedback_rows(), t.text, t.select)
    fb.save(settings.feedback_path, items)
    counts = {}
    for f in items:
        counts[f.label] = counts.get(f.label, 0) + 1
    print(f"{len(items)} labeled Radar rows -> {settings.feedback_path}: {counts}")
    print(fb.few_shot_block(items, settings.few_shot) or "(no few-shot examples yet)")


def cmd_retune(settings: Settings, write: bool, offline: bool) -> None:
    """Monthly: sweep the cutoff against Oscar's own decisions. No model calls."""
    import json as _json
    from . import feedback as fb
    if offline:
        items = fb.load(settings.feedback_path)
    else:
        from .notion_out import NotionTracker
        _clean_env()
        t = NotionTracker(settings.notion_token, settings.notion_database_id)
        items = fb.from_rows(t.feedback_rows(), t.text, t.select)
        fb.save(settings.feedback_path, items)
    today = dt.date.today()
    rec = fb.recommend(items, settings.fit_threshold)
    sw = fb.sweep(items)
    md = fb.retune_markdown(rec, sw, fb.local_rates(items), today)
    settings.reports_dir.mkdir(exist_ok=True)
    (settings.reports_dir / f"retune_{today.isoformat()}.md").write_text(md)
    print(md)
    if write:
        prev = fb.load_tuning(settings.tuning_path)
        settings.tuning_path.write_text(_json.dumps({
            "fit_threshold": rec["recommended"], "previous": rec["current"],
            "updated": today.isoformat(), "n_labels": rec["n_labels"],
            "n_positive": rec["n_positive"], "reason": rec["reason"],
            "history": (prev.get("history") or [])[-11:] + [
                {"date": today.isoformat(), "fit_threshold": rec["recommended"]}],
        }, indent=2) + "\n")
        print(f"wrote {settings.tuning_path}")


def _write_report(settings, results, meter, fetched, prefiltered) -> None:
    settings.reports_dir.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    (settings.reports_dir / f"run_{stamp}.json").write_text(json.dumps(
        {"fetched": fetched, "prefiltered": prefiltered, "cost_usd": round(meter.spent, 4),
         "by_model": meter.by_model, "results": results}, indent=2, ensure_ascii=False))
    lines = [f"# Radar run {stamp}", "",
             f"Fetched {fetched} · new after title filter {prefiltered} · "
             f"scored {len(results)} · cost ${meter.spent:.4f}", "",
             "| Score | Fit | Where | Company | Title | Lane |", "|---|---|---|---|---|---|"]
    for r in sorted(results, key=lambda r: (locality.rank(r.get("tier", "other")), -r["score"]["fit_score"])):
        s, p = r["score"], r["posting"]
        where = locality.LABELS.get(r.get("tier", "other"), "")
        lines.append(f"| {s['fit_score']} | {s['fit']} | {where} | {p['company']} | "
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
    k = sub.add_parser("kits")
    k.add_argument("--limit", type=int, default=40)
    k.add_argument("--min-score", type=int, default=50)
    k.add_argument("--model", default="claude-opus-5-5")
    k.add_argument("--judge", default="claude-opus-5-5")
    k.add_argument("--max-usd", type=float, default=30.0)
    k.add_argument("--write-notion", action="store_true")
    k.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None)
    k.add_argument("--revise", action="store_true", help="fix judge-flagged claims, then re-judge")
    k.add_argument("--redo", action="store_true", help="include rows that already have a kit")
    k.add_argument("--label", default="", help='e.g. "v2" to mark a regenerated kit')
    pp = sub.add_parser("prep")
    pp.add_argument("--limit", type=int, default=40)
    pp.add_argument("--min-score", type=int, default=50)
    pp.add_argument("--model", default="claude-opus-5-5")
    pp.add_argument("--judge", default="claude-opus-5-5")
    pp.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default="high")
    pp.add_argument("--max-usd", type=float, default=60.0)
    pp.add_argument("--write-notion", action="store_true")
    pp.add_argument("--no-revise", action="store_true")
    sub.add_parser("feedback")
    rt = sub.add_parser("retune")
    rt.add_argument("--write", action="store_true", help="save the new cutoff to config/tuning.json")
    rt.add_argument("--offline", action="store_true", help="use data/feedback.jsonl instead of Notion")
    wk = sub.add_parser("weekly")
    wk.add_argument("--write-notion", action="store_true")
    wk.add_argument("--days", type=int, default=7)
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
    elif a.cmd == "kits":
        from .kits import run_kits
        run_kits(settings, a.limit, a.model, a.judge, a.min_score, a.max_usd, a.write_notion,
                 effort=a.effort, revise=a.revise, redo=a.redo, label=a.label)
    elif a.cmd == "prep":
        from .prep import run_prep
        run_prep(settings, a.limit, a.model, a.judge, a.min_score, a.effort, a.max_usd, a.write_notion,
                 revise=not a.no_revise)
    elif a.cmd == "feedback":
        cmd_feedback(settings)
    elif a.cmd == "retune":
        cmd_retune(settings, a.write, a.offline)
    elif a.cmd == "weekly":
        from .weekly import run_weekly
        _clean_env()
        run_weekly(settings, a.write_notion, a.days)
    elif a.cmd == "sweep":
        from .sweep import run_sweep
        run_sweep(settings, a.limit, a.model, a.write_notion)


if __name__ == "__main__":
    main()
