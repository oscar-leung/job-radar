import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

LABELS = ROOT / "evals" / "labels.jsonl"


def load_labels() -> list[dict]:
    if not LABELS.exists():
        return []
    return [json.loads(l) for l in LABELS.read_text().splitlines() if l.strip()]


@pytest.fixture(scope="session")
def labels():
    rows = load_labels()
    if not rows:
        pytest.skip("evals/labels.jsonl not found — label postings first (see evals/README.md)")
    return rows


@pytest.fixture(scope="session")
def live_client():
    if not os.getenv("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")
    import anthropic
    return anthropic.Anthropic()
