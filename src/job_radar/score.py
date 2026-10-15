"""Two-stage LLM matching: cheap relevance filter, then structured fit scoring.

Both stages force a tool call so output is schema-validated JSON, and both put the
resume + rubric in a cached system block so repeated calls pay ~5% for that prefix.
"""
from __future__ import annotations

from pathlib import Path

from .ingest import Posting

LANES = ["QA/SDET", "AI Eval", "SWE", "Data", "IT Spec", "Research", "Other"]
FITS = ["🟢 Strong", "🟡 Stretch", "🎯 Reach", "⚪ Skip"]
LOCATIONS = ["Davis", "Sacramento", "Yolo", "Bay Area", "Remote-US", "Remote-Global", "Other"]
RESUME_VERSIONS = ["QA-SDET", "AI-Eval", "SWE", "General"]

FILTER_TOOL = {
    "name": "record_relevance",
    "description": "Record whether this posting is worth a full fit review.",
    "input_schema": {
        "type": "object",
        "properties": {
            "relevant": {"type": "boolean"},
            "reason": {"type": "string", "description": "One short sentence."},
        },
        "required": ["relevant", "reason"],
    },
}

SCORE_TOOL = {
    "name": "record_fit",
    "description": "Record the structured fit assessment for this posting.",
    "input_schema": {
        "type": "object",
        "properties": {
            "fit_score": {"type": "integer", "minimum": 0, "maximum": 100},
            "fit": {"type": "string", "enum": FITS},
            "lane": {"type": "string", "enum": LANES},
            "location": {"type": "string", "enum": LOCATIONS},
            "resume_version": {"type": "string", "enum": RESUME_VERSIONS},
            "reasons": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
            "gaps": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
            "bullets": {
                "type": "array", "items": {"type": "string"},
                "minItems": 3, "maxItems": 3,
                "description": "Tailored resume bullets using ONLY facts present in the resume.",
            },
        },
        "required": ["fit_score", "fit", "lane", "location", "resume_version",
                     "reasons", "gaps", "bullets"],
    },
}


def load_prompt(prompts_dir: Path, name: str) -> str:
    return (prompts_dir / name).read_text()


def system_blocks(rubric: str, resume: str) -> list[dict]:
    return [{
        "type": "text",
        "text": f"{rubric}\n\n<resume>\n{resume}\n</resume>",
        "cache_control": {"type": "ephemeral"},
    }]


def posting_message(p: Posting, max_chars: int = 12000) -> str:
    desc = p.description[:max_chars]
    return (
        f"<posting>\nCompany: {p.company}\nTitle: {p.title}\n"
        f"Location: {p.location}\nURL: {p.url}\n\n{desc}\n</posting>"
    )


def _tool_input(resp, name: str) -> dict:
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == name:
            return dict(block.input)
    raise ValueError(f"model did not call {name}")


def filter_params(model: str, system: list[dict], p: Posting) -> dict:
    return {
        "model": model, "max_tokens": 200, "system": system,
        "tools": [FILTER_TOOL],
        "tool_choice": {"type": "tool", "name": FILTER_TOOL["name"]},
        "messages": [{"role": "user", "content": posting_message(p, 4000)}],
    }


def score_params(model: str, system: list[dict], p: Posting) -> dict:
    return {
        "model": model, "max_tokens": 1200, "system": system,
        "tools": [SCORE_TOOL],
        "tool_choice": {"type": "tool", "name": SCORE_TOOL["name"]},
        "messages": [{"role": "user", "content": posting_message(p)}],
    }


def is_relevant(client, model: str, system: list[dict], p: Posting):
    resp = client.messages.create(**filter_params(model, system, p))
    return _tool_input(resp, FILTER_TOOL["name"]), resp.usage


def score(client, model: str, system: list[dict], p: Posting):
    resp = client.messages.create(**score_params(model, system, p))
    return _tool_input(resp, SCORE_TOOL["name"]), resp.usage


def parse_message(message, name: str) -> dict:
    """Extract tool input from a Batch API result message."""
    return _tool_input(message, name)
