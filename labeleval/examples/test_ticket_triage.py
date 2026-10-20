"""Example: grade three "is this support ticket urgent?" scorers against human labels.

The 30 tickets and labels are synthetic, written for this demo. Run:

    pytest examples/ -v                      # two free keyword scorers
    ANTHROPIC_API_KEY=... pytest examples/ -v -m llm_eval   # plus a Claude scorer

The keyword baseline fails its bar on purpose: the failure message lists exactly which
tickets it got wrong, which is the point of grading against people.
"""
import json
import os
import re
from pathlib import Path

import pytest

HERE = Path(__file__).parent


@pytest.fixture(scope="module")
def cases():
    from labeleval import load_cases
    return load_cases(HERE / "labels.jsonl", HERE / "tickets.jsonl")


def naive_keywords(item: dict) -> float:
    """Baseline: trusts the word 'urgent'. Easy to fool."""
    return 1.0 if re.search(r"urgent|critical|asap", item["text"], re.I) else 0.0


IMPACT = re.compile(r"\b(all|every|entire|anyone|customers?|production|down|fail\w*|"
                    r"crash\w*|missing|security|charged twice|timing out|stopped|no results|"
                    r"locked out|losing)\b", re.I)


def impact_keywords(item: dict) -> float:
    """Counts signs of breadth and breakage instead of trusting the word 'urgent'."""
    return float(len(IMPACT.findall(item["text"])))


def test_naive_baseline_fails_and_says_why(labeleval, cases):
    r = labeleval.evaluate("naive-keywords", cases, naive_keywords, threshold=1)
    assert r.confusion.recall < 0.3  # misses most real incidents
    assert "t17" in r.misses(cases)["false_positives"]  # "URGENT!!! love the new release"


def test_impact_scorer_meets_bar(labeleval, cases):
    r = labeleval.evaluate("impact-keywords", cases, impact_keywords, threshold=1)
    labeleval.assert_meets(r, cases, min_precision=0.8, min_recall=0.8)


@pytest.mark.llm_eval
def test_claude_scorer(labeleval, cases):
    anthropic = pytest.importorskip("anthropic")
    if not os.getenv("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic()
    schema = {"type": "object", "additionalProperties": False, "required": ["urgency"],
              "properties": {"urgency": {"type": "integer",
                                         "description": "0 = can wait, 100 = drop everything"}}}

    def claude(item: dict) -> float:
        msg = client.messages.create(
            model=os.getenv("LABELEVAL_MODEL", "claude-haiku-5-5"), max_tokens=100,
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content":
                       "Rate how urgently support must act on this ticket. Urgent means real, "
                       f"broad, or security impact right now, not tone.\n\nTicket: {item['text']}"}])
        return json.loads(msg.content[0].text)["urgency"]

    r = labeleval.evaluate("claude", cases, claude, threshold=50)
    labeleval.assert_meets(r, cases, min_precision=0.8, min_recall=0.8)
