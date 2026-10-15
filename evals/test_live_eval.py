"""Live eval: does the scorer agree with hand labels?  (costs credits)

    pytest -m live evals/test_live_eval.py

Gold label = "good" means Oscar would apply. Prediction = fit_score >= threshold.
Thresholds below are the regression bar; raise them as the prompt improves.
"""
import json
import os
from pathlib import Path

import pytest

from job_radar import score as sc
from job_radar.config import Settings, load_resume
from job_radar.cost import CostMeter
from job_radar.ingest import Posting
from job_radar.metrics import confusion, precision_recall

MIN_PRECISION = float(os.getenv("EVAL_MIN_PRECISION", "0.75"))
MIN_RECALL = float(os.getenv("EVAL_MIN_RECALL", "0.70"))
RESULTS = Path(__file__).parent / "results"


@pytest.mark.live
def test_scorer_matches_hand_labels(labels, live_client):
    s = Settings()
    system = sc.system_blocks(sc.load_prompt(s.prompts_dir, "scoring_rubric.md"), load_resume(s))
    meter = CostMeter(budget_usd=float(os.getenv("EVAL_MAX_USD", "5")))
    gold, pred, rows = [], [], []

    for lab in labels:
        p = Posting(**{k: lab[k] for k in
                       ("source", "company", "external_id", "title", "location", "url", "description")})
        out, usage = sc.score(live_client, s.model_score, system, p)
        meter.add(s.model_score, usage)
        gold.append(lab["label"] == "good")
        pred.append(out["fit_score"] >= s.fit_threshold)
        rows.append({"id": p.external_id, "gold": lab["label"], "score": out["fit_score"],
                     "fit": out["fit"], "lane_pred": out["lane"], "lane_gold": lab.get("lane")})
        if meter.over_budget:
            pytest.fail(f"eval budget exceeded at {len(rows)} postings")

    c = confusion(gold, pred)
    precision, recall, f1 = precision_recall(c)
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"live_{s.model_score}.json").write_text(json.dumps({
        "model": s.model_score, "n": len(rows), "threshold": s.fit_threshold,
        "confusion": c, "precision": precision, "recall": recall, "f1": f1,
        "cost_usd": round(meter.spent, 4), "rows": rows}, indent=2, ensure_ascii=False))

    assert precision >= MIN_PRECISION, f"precision {precision:.2f} < {MIN_PRECISION}"
    assert recall >= MIN_RECALL, f"recall {recall:.2f} < {MIN_RECALL}"
