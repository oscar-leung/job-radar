import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

LABELS = ROOT / "evals" / "labels.jsonl"
SNAPSHOT = ROOT / "evals" / "postings_snapshot.jsonl"


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []


def load_labels(labels_path: Path = LABELS, snapshot_path: Path = SNAPSHOT) -> list[dict]:
    """Labels may be full rows, or compact {external_id, label, lane} rows that are
    joined to the public postings snapshot (keeps hand labels out of the public repo)."""
    snap = {p["external_id"]: p for p in _read_jsonl(snapshot_path)}
    out = []
    for row in _read_jsonl(labels_path):
        if "description" not in row:
            if row["external_id"] not in snap:
                continue
            row = {**snap[row["external_id"]], **row}
        out.append(row)
    return out


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
