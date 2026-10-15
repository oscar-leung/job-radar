"""Pull real postings and label them good/bad in the terminal → evals/labels.jsonl.

    python evals/label_helper.py --n 50

Keys: g = good (I'd apply), b = bad, s = skip, q = quit. Free — no model calls.
Aim for a mix: ~20 good, ~30 bad, including tricky near-misses.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from job_radar.config import Settings  # noqa: E402
from job_radar.ingest import FETCHERS, fetch_all, keyword_prefilter  # noqa: E402

OUT = ROOT / "evals" / "labels.jsonl"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--no-prefilter", action="store_true",
                    help="include titles the keyword filter would drop (gives harder negatives)")
    a = ap.parse_args()
    s = Settings()
    cfg = yaml.safe_load(s.companies_path.read_text())
    posts = fetch_all({k: cfg.get(k) for k in FETCHERS})
    if not a.no_prefilter:
        posts = keyword_prefilter(posts, cfg.get("title_include", []), cfg.get("title_exclude", []))
    seen = set()
    if OUT.exists():
        seen = {json.loads(l)["external_id"] for l in OUT.read_text().splitlines() if l.strip()}
    posts = [p for p in posts if p.external_id not in seen]
    random.shuffle(posts)

    done = 0
    with OUT.open("a") as f:
        for p in posts:
            if done >= a.n:
                break
            print(f"\n[{done + 1}/{a.n}] {p.company} — {p.title} ({p.location})\n{p.url}\n"
                  f"{p.description[:800]}...\n")
            k = input("g/b/s/q > ").strip().lower()
            if k == "q":
                break
            if k not in ("g", "b"):
                continue
            row = p.to_dict() | {"label": "good" if k == "g" else "bad"}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            done += 1
    print(f"labeled {done} → {OUT}")


if __name__ == "__main__":
    main()
