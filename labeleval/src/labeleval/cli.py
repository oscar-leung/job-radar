"""labeleval CLI — grade saved scores against labels without writing any code.

    labeleval report labels.jsonl scores_a.jsonl scores_b.jsonl --threshold 50
    labeleval report labels.jsonl scores.jsonl --sweep 30 90 5

Scores files are JSONL rows of {"id": ..., "score": ...}; the scorer name is the filename.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import core


def _scores(path: Path) -> dict[str, float]:
    return {str(r["id"]): float(r["score"]) for r in
            (json.loads(l) for l in path.read_text().splitlines() if l.strip())}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="labeleval")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="compare one or more score files against labels")
    r.add_argument("labels")
    r.add_argument("scores", nargs="+")
    r.add_argument("--threshold", type=float, default=0.5)
    r.add_argument("--sweep", type=float, nargs=3, metavar=("START", "STOP", "STEP"),
                   help="thresholds to try for the best-cutoff column")
    a = ap.parse_args(argv)

    cases = core.load_cases(a.labels)
    ths = None
    if a.sweep:
        start, stop, step = a.sweep
        ths, t = [], start
        while t <= stop + 1e-9:
            ths.append(round(t, 6))
            t += step
    reports = [core.evaluate(Path(p).stem, cases, scores=_scores(Path(p)),
                             threshold=a.threshold, thresholds=ths) for p in a.scores]
    print(core.compare(reports))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
