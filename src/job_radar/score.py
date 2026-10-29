"""Two-stage LLM matching: cheap relevance filter, then structured fit scoring.

Both stages use structured outputs (output_config.format = json_schema), so the reply is
schema-constrained JSON, and both put the resume + rubric in a cached system block so
repeated calls pay a fraction for that prefix. (Claude 5.5 models reject forced tool_choice.)
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from .ingest import Posting

LANES = ["QA/SDET", "AI Eval", "SWE", "Data", "IT Spec", "Educator", "Research", "Other"]
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


def system_blocks(rubric: str, resume: str, examples: str = "") -> list[dict]:
    """One cached system block: rubric, optional few-shot feedback (Oscar's own decisions on
    past Radar matches, see feedback.py), then the resume. The feedback changes at most once a
    day, so the whole prefix stays cache-friendly within a run."""
    extra = f"\n\n{examples.strip()}" if examples.strip() else ""
    return [{
        "type": "text",
        "text": f"{rubric}{extra}\n\n<resume>\n{resume}\n</resume>",
        "cache_control": {"type": "ephemeral"},
    }]


def posting_message(p: Posting, max_chars: int = 12000) -> str:
    desc = p.description[:max_chars]
    return (
        f"<posting>\nCompany: {p.company}\nTitle: {p.title}\n"
        f"Location: {p.location}\nURL: {p.url}\n\n{desc}\n</posting>"
    )


_UNSUPPORTED = ("minimum", "maximum", "maxItems", "minLength", "maxLength", "multipleOf")


def json_schema(tool: dict) -> dict:
    """Tool input_schema -> structured-output schema: additionalProperties false on every
    object, unsupported numeric/array constraints moved into descriptions (enforced in _normalize)."""
    def walk(node):
        if isinstance(node, dict):
            notes = []
            for k in _UNSUPPORTED:
                if k in node:
                    notes.append(f"{k} {node.pop(k)}")
            if node.get("minItems", 0) > 1:
                notes.append(f"exactly/at least {node.pop('minItems')} items")
            if notes:
                node["description"] = (node.get("description", "") + " (" + ", ".join(notes) + ")").strip()
            if node.get("type") == "object":
                node["additionalProperties"] = False
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        return node
    return walk(copy.deepcopy(tool["input_schema"]))


def output_config(tool: dict, effort: str | None = None) -> dict:
    """Structured-output config; `effort` (low|medium|high|xhigh|max) sets how hard the model
    thinks before answering. Omitted = the model's default."""
    cfg = {"format": {"type": "json_schema", "schema": json_schema(tool)}}
    if effort:
        cfg["effort"] = effort
    return cfg


def _canon(value, options):
    for o in options:
        if isinstance(value, str) and value.casefold() == o.casefold():
            return o
    return value


def _normalize(name: str, out: dict) -> dict:
    """Re-apply constraints structured outputs can't express; fix enum casing."""
    if name == SCORE_TOOL["name"]:
        out["fit_score"] = max(0, min(100, int(out.get("fit_score", 0))))
        out["fit"] = _canon(out.get("fit"), FITS)
        out["lane"] = _canon(out.get("lane"), LANES)
        out["location"] = _canon(out.get("location"), LOCATIONS)
        out["resume_version"] = _canon(out.get("resume_version"), RESUME_VERSIONS)
        out["reasons"] = list(out.get("reasons", []))[:4]
        out["gaps"] = list(out.get("gaps", []))[:4]
        out["bullets"] = list(out.get("bullets", []))[:3]
    return out


def _tool_input(resp, name: str) -> dict:
    """Read the structured JSON reply (text block); tool_use kept for old batch results."""
    if getattr(resp, "stop_reason", None) == "refusal":
        raise ValueError(f"model refused ({name})")
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == name:
            return _normalize(name, dict(block.input))
    for block in resp.content:
        if getattr(block, "type", None) == "text" and block.text.strip():
            return _normalize(name, json.loads(block.text))
    raise ValueError(f"no structured output for {name}")


def filter_params(model: str, system: list[dict], p: Posting) -> dict:
    return {
        "model": model, "max_tokens": 1024, "system": system,
        "output_config": output_config(FILTER_TOOL),
        "messages": [{"role": "user", "content": FILTER_TOOL["description"] + "\n\n" + posting_message(p, 4000)}],
    }


def score_params(model: str, system: list[dict], p: Posting, effort: str | None = None) -> dict:
    return {
        "model": model, "max_tokens": 16000 if effort in ("high", "xhigh", "max") else 3000,
        "system": system,
        "output_config": output_config(SCORE_TOOL, effort),
        "messages": [{"role": "user", "content": SCORE_TOOL["description"] + "\n\n" + posting_message(p)}],
    }


def _call(client, params: dict, name: str):
    """One call; if the reply hit max_tokens (truncated JSON), retry once with double room.
    Returns (parsed, [usage, ...]) so every attempt is metered."""
    usages = []
    for attempt in range(2):
        resp = client.messages.create(**params)
        usages.append(resp.usage)
        if getattr(resp, "stop_reason", None) == "max_tokens" and attempt == 0:
            params = {**params, "max_tokens": params["max_tokens"] * 2}
            continue
        return _tool_input(resp, name), usages
    return _tool_input(resp, name), usages


def is_relevant(client, model: str, system: list[dict], p: Posting):
    out, usages = _call(client, filter_params(model, system, p), FILTER_TOOL["name"])
    return out, usages


def score(client, model: str, system: list[dict], p: Posting):
    out, usages = _call(client, score_params(model, system, p), SCORE_TOOL["name"])
    return out, usages


def parse_message(message, name: str) -> dict:
    """Extract tool input from a Batch API result message."""
    return _tool_input(message, name)
