"""Compare scoring models on the labeled set via the Message Batches API (50% off),
then have a judge model check every tailored bullet for fabrication.

    python evals/compare_models.py                       # all four models + judge
    python evals/compare_models.py --models claude-haiku-5-5 claude-sonnet-5-5
    python evals/compare_models.py --no-judge

Writes evals/results/comparison.json and evals/results/comparison.md (README-ready table).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals"))

import anthropic  # noqa: E402

from conftest import load_labels  # noqa: E402
from job_radar import score as sc  # noqa: E402
from job_radar.config import Settings, load_resume  # noqa: E402
from job_radar.cost import CostMeter  # noqa: E402
from job_radar.ingest import Posting  # noqa: E402
from job_radar.metrics import confusion, precision_recall  # noqa: E402

DEFAULT_MODELS = ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1"]
OUT = ROOT / "evals" / "results"

JUDGE_TOOL = {
    "name": "record_faithfulness",
    "description": "Judge each bullet against the resume.",
    "input_schema": {"type": "object", "properties": {
        "verdicts": {"type": "array", "items": {"type": "object", "properties": {
            "bullet_index": {"type": "integer"},
            "supported": {"type": "boolean"},
            "unsupported_claims": {"type": "array", "items": {"type": "string"}},
        }, "required": ["bullet_index", "supported", "unsupported_claims"]}},
    }, "required": ["verdicts"]},
}
JUDGE_SYSTEM = ("You audit resume bullets for fabrication. A bullet is supported only if every "
                "employer, tool, metric, title, and outcome in it appears in the resume (rewording "
                "is fine). Flag anything invented or inflated. Answer in the required JSON format.")


def run_batch(client, reqs: list[dict]):
    b = client.messages.batches.create(requests=reqs)
    print(f"  batch {b.id}: {len(reqs)} requests")
    while client.messages.batches.retrieve(b.id).processing_status != "ended":
        time.sleep(20)
    return {r.custom_id: r.result for r in client.messages.batches.results(b.id)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    ap.add_argument("--judge", default=None)
    ap.add_argument("--no-judge", action="store_true")
    a = ap.parse_args()

    s = Settings()
    judge = a.judge or s.model_judge
    labels = load_labels()
    if not labels:
        raise SystemExit("evals/labels.jsonl is empty — label postings first.")
    resume = load_resume(s)
    system = sc.system_blocks(sc.load_prompt(s.prompts_dir, "scoring_rubric.md"), resume)
    client = anthropic.Anthropic()
    postings = [Posting(**{k: l[k] for k in ("source", "company", "external_id", "title",
                                              "location", "url", "description")}) for l in labels]

    summary = []
    for model in a.models:
        print(f"[{model}] scoring {len(postings)} postings")
        meter = CostMeter(budget_usd=1e9)
        reqs = [{"custom_id": f"p{i}", "params": sc.score_params(model, system, p)}
                for i, p in enumerate(postings)]
        res = run_batch(client, reqs)
        outs, gold, pred = {}, [], []
        for i, lab in enumerate(labels):
            r = res.get(f"p{i}")
            if not r or r.type != "succeeded":
                continue
            meter.add(model, r.message.usage, batch=True)
            o = sc.parse_message(r.message, "record_fit")
            outs[i] = o
            gold.append(lab["label"] == "good")
            pred.append(o["fit_score"] >= s.fit_threshold)
        c = confusion(gold, pred)
        p_, r_, f1 = precision_recall(c)
        row = {"model": model, "n": len(gold), "precision": p_, "recall": r_, "f1": f1,
               "score_cost_usd": meter.spent}

        if not a.no_judge:
            jm = CostMeter(budget_usd=1e9)
            jreqs = [{"custom_id": f"j{i}", "params": {
                "model": judge, "max_tokens": 800,
                "system": [{"type": "text", "text": f"{JUDGE_SYSTEM}\n\n<resume>\n{resume}\n</resume>",
                            "cache_control": {"type": "ephemeral"}}],
                "output_config": sc.output_config(JUDGE_TOOL),
                "messages": [{"role": "user", "content": JUDGE_TOOL["description"] + "\n\nBullets:\n" + "\n".join(
                    f"{k}. {b}" for k, b in enumerate(o["bullets"]))}],
            }} for i, o in outs.items()]
            jres = run_batch(client, jreqs)
            total = supported = 0
            for r in jres.values():
                if r.type != "succeeded":
                    continue
                jm.add(judge, r.message.usage, batch=True)
                for v in sc.parse_message(r.message, JUDGE_TOOL["name"])["verdicts"]:
                    total += 1
                    supported += bool(v["supported"])
            row["faithfulness"] = supported / total if total else None
            row["judge_cost_usd"] = jm.spent
        summary.append(row)
        print(f"  {row}")

    OUT.mkdir(exist_ok=True)
    (OUT / "comparison.json").write_text(json.dumps(summary, indent=2))
    md = ["| Model | n | Precision | Recall | F1 | Bullet faithfulness | Scoring cost (batch) |",
          "|---|---|---|---|---|---|---|"]
    for r in summary:
        faith = f"{r['faithfulness']:.0%}" if r.get("faithfulness") is not None else "—"
        md.append(f"| {r['model']} | {r['n']} | {r['precision']:.2f} | {r['recall']:.2f} | "
                  f"{r['f1']:.2f} | {faith} | ${r['score_cost_usd']:.3f} |")
    (OUT / "comparison.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
