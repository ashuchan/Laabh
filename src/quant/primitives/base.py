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
    warmup_minutes: int

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
            history: Prior bars (oldest first), length may be < warmup_minutes
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
        return len(history) >= self.warmup_minutes

    @staticmethod
    def _clamp(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
        return max(lo, min(hi, value))

    @staticmethod
    def _tanh_strength(x: float) -> float:
        return math.tanh(x)
