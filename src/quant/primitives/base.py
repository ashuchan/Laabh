"""Abstract base class for all intraday signal primitives."""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

from src.quant.feature_store import FeatureBundle


@dataclass
class Signal:
    """Output of a primitive's compute_signal() call."""

    direction: Literal["bullish", "bearish", "neutral"]
    strength: float                     # |strength| ∈ [0, 1]
    strategy_class: str                 # "long_call", "long_put", "debit_call_spread", ...
    expected_horizon_minutes: int
    expected_vol_pct: float             # to drive trailing-stop sizing


class BasePrimitive(ABC):
    """Contract every primitive must satisfy."""

    name: str
    # Number of *3-min bars* of history this primitive needs before it can
    # emit a signal. Field name was historically ``warmup_minutes`` but the
    # value was always a bar count — see commit history. The orchestrator
    # used to divide by 3 (treating it as minutes), which permanently
    # blocked momentum (needs 11 bars) and vol_breakout (needs 20 bars).
    warmup_bars: int

    @abstractmethod
    def compute_signal(
        self,
        features: FeatureBundle,
        history: list[FeatureBundle],
        *,
        trace: dict | None = None,
    ) -> Signal | None:
        """Return a Signal with strength ∈ [-1, 1] or None if no signal.

        Args:
            features: Current-bar feature bundle.
            history: Prior bars (oldest first), length may be < warmup_bars
                     during startup. Return None if warmup not yet satisfied.
            trace: When non-None, the primitive populates this dict with
                   inputs / intermediates / a human-readable formula. Used by
                   the Decision Inspector to render formula cards. Caller
                   owns the dict; primitive only mutates. Live mode passes
                   None, so there is zero overhead in production.

        Trace shape (when populated):
            {"name": <primitive name>,
             "inputs": {<feature name>: value, ...},
             "intermediates": {<derived name>: value, ...},
             "formula": "<human-readable formula with values plugged in>"}
        """

    def _past_warmup(self, history: list[FeatureBundle]) -> bool:
        """Return True once enough history has accumulated."""
        return len(history) >= self.warmup_bars

    def should_take_profit(
        self,
        position,
        current_features: FeatureBundle,
    ) -> bool:
        """Should the orchestrator close this position because the primitive's
        entry hypothesis has been fulfilled?

        Default: ``False`` — the generic exit policy in ``src/quant/exits.py``
        (trailing stop, profit ratchet, time stop, signal flip) handles the
        cuts. Override when the primitive defines a "we got what we came for"
        moment — e.g. a mean-reversion primitive should close once price has
        reverted toward its anchor, regardless of whether the trailing stop
        has triggered yet.

        ``position`` is an ``src.quant.exits.OpenPosition``; left untyped
        here to avoid a circular import. ``current_features`` is the
        latest tick's FeatureBundle for the position's symbol.
        """
        return False

    @staticmethod
    def _clamp(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
        return max(lo, min(hi, value))

    @staticmethod
    def _tanh_strength(x: float) -> float:
        return math.tanh(x)
