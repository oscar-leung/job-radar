"""Per-call cost accounting from API usage objects (USD per million tokens)."""
from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

# input, output, 5m cache write, cache read  — Claude Platform pricing, Oct 2026
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-haiku-5-5":  (0.10, 0.50, 0.125, 0.01),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.10),
    "claude-opus-5-5":   (4.00, 20.00, 5.00, 0.20),
    "claude-fable-5-1":  (10.00, 50.00, 12.50, 0.25),
}


def _u(usage, name: str) -> int:
    if usage is None:
        return 0
    val = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, 0)
    return int(val or 0)


def call_cost(model: str, usage, batch: bool = False) -> float:
    """Cost of one request. Batch halves every token category.

    Note: Haiku 5.5 prompts over 100K tokens bill at a higher tier; job postings
    never get close, so that tier is not modeled here.
    """
    if model not in PRICES:
        raise KeyError(f"no price for {model}; add it to cost.PRICES")
    p_in, p_out, p_write, p_read = PRICES[model]
    usd = (
        _u(usage, "input_tokens") * p_in
        + _u(usage, "output_tokens") * p_out
        + _u(usage, "cache_creation_input_tokens") * p_write
        + _u(usage, "cache_read_input_tokens") * p_read
    ) / 1_000_000
    return usd * (0.5 if batch else 1.0)


@dataclass
class CostMeter:
    """Tracks spend for one run and enforces a hard budget."""
    budget_usd: float
    spent: float = 0.0
    calls: int = 0
    by_model: dict[str, float] = field(default_factory=dict)

    def add(self, model: str, usage, batch: bool = False) -> float:
        c = call_cost(model, usage, batch)
        self.spent += c
        self.calls += 1
        self.by_model[model] = self.by_model.get(model, 0.0) + c
        return c

    @property
    def over_budget(self) -> bool:
        return self.spent >= self.budget_usd

    def log(self, path: Path, label: str) -> None:
        new = not path.exists()
        with path.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["timestamp", "label", "calls", "usd", "by_model"])
            w.writerow([
                dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                label, self.calls, f"{self.spent:.4f}",
                ";".join(f"{m}={v:.4f}" for m, v in self.by_model.items()),
            ])
