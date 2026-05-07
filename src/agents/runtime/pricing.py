"""Model pricing table and cost computation utilities."""
from __future__ import annotations

from decimal import Decimal

# USD per 1M tokens, input / output / cache_write / cache_read
# Confirm against current Anthropic pricing at deploy time.
MODEL_PRICES: dict[str, dict[str, Decimal]] = {
    "claude-haiku-4-5-20251001": {
        "input": Decimal("0.80"),
        "output": Decimal("4.00"),
        "cache_write_input": Decimal("1.00"),
        "cache_read_input": Decimal("0.08"),
    },
    "claude-sonnet-4-6": {
        "input": Decimal("3.00"),
        "output": Decimal("15.00"),
        "cache_write_input": Decimal("3.75"),
        "cache_read_input": Decimal("0.30"),
    },
    "claude-opus-4-7": {
        "input": Decimal("15.00"),
        "output": Decimal("75.00"),
        "cache_write_input": Decimal("18.75"),
        "cache_read_input": Decimal("1.50"),
    },
}

_MILLION = Decimal("1_000_000")


def compute_cost(model: str, usage: object) -> Decimal:
    """Compute USD cost from a model name and an Anthropic usage object.

    Falls back to the cheapest known model if the model is not in the table.
    Uses Decimal throughout — never float.
    """
    prices = MODEL_PRICES.get(model, MODEL_PRICES["claude-haiku-4-5-20251001"])

    input_tokens = Decimal(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = Decimal(getattr(usage, "output_tokens", 0) or 0)
    cache_read = Decimal(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cache_write = Decimal(getattr(usage, "cache_creation_input_tokens", 0) or 0)

    # Cache reads replace some input tokens; adjust the billable input accordingly.
    # Clamp to 0 — cache_read can exceed input_tokens when the entire prompt is cached.
    billable_input = max(Decimal("0"), input_tokens - cache_read)

    cost = (
        billable_input / _MILLION * prices["input"]
        + output_tokens / _MILLION * prices["output"]
        + cache_write / _MILLION * prices["cache_write_input"]
        + cache_read / _MILLION * prices["cache_read_input"]
    )
    return cost


def project_agent_cost(model: str, max_input_tokens: int, max_output_tokens: int) -> Decimal:
    """Worst-case cost estimate (no caching) for one agent call."""
    prices = MODEL_PRICES.get(model, MODEL_PRICES["claude-haiku-4-5-20251001"])
    return (
        Decimal(max_input_tokens) / _MILLION * prices["input"]
        + Decimal(max_output_tokens) / _MILLION * prices["output"]
    )
