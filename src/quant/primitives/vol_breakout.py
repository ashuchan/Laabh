"""Volatility Breakout primitive — Bollinger Band width expansion.

bb_width_now > 1.5 × bb_width_20bar_avg → breakout regime.
Direction = sign(LTP - 20-bar SMA).
Strength = tanh((bb_width_now / bb_width_avg) - 1).
"""
from __future__ import annotations

import math

from src.quant.feature_store import FeatureBundle
from src.quant.primitives.base import BasePrimitive, Signal

_WARMUP_BARS = 20
_BB_EXPANSION_RATIO = 1.5


class VolBreakoutPrimitive(BasePrimitive):
    """Bollinger Band width expansion breakout."""

    name = "vol_breakout"
    warmup_minutes = _WARMUP_BARS

    def compute_signal(
        self,
        features: FeatureBundle,
        history: list[FeatureBundle],
        *,
        trace: dict | None = None,
    ) -> Signal | None:
        if not self._past_warmup(history):
            return None

        bb_now = features.bb_width
        if bb_now <= 0:
            return None

        # Average BB width over the history window
        bb_avg = _bb_avg(history)
        if bb_avg <= 0:
            return None

        if bb_now < _BB_EXPANSION_RATIO * bb_avg:
            return None  # No expansion

        # Direction: sign of (LTP - 20-bar SMA)
        sma20 = _sma([b.underlying_ltp for b in history[-_WARMUP_BARS:]])
        ltp = features.underlying_ltp
        if sma20 == 0:
            return None

        strength = self._clamp(self._tanh_strength((bb_now / bb_avg) - 1.0))

        if trace is not None:
            trace["name"] = self.name
            trace["inputs"] = {
                "bb_width_now": float(bb_now),
                "ltp": float(ltp),
                "rv_30min": float(features.realized_vol_30min),
            }
            trace["intermediates"] = {
                "bb_avg": float(bb_avg),
                "bb_ratio": float(bb_now / bb_avg),
                "sma20": float(sma20),
                "expansion_threshold": float(_BB_EXPANSION_RATIO),
            }
            trace["formula"] = (
                f"bb_ratio = bb_now / bb_avg = {bb_now:.4f} / {bb_avg:.4f} "
                f"= {bb_now / bb_avg:.4f} > {_BB_EXPANSION_RATIO}; "
                f"strength = tanh(bb_ratio − 1) = {strength:.4f}"
            )

        if ltp >= sma20:
            return Signal(
                direction="bullish",
                strength=strength,
                strategy_class="debit_call_spread",
                expected_horizon_minutes=15,
                expected_vol_pct=features.realized_vol_30min,
            )
        return Signal(
            direction="bearish",
            strength=strength,
            strategy_class="debit_put_spread",
            expected_horizon_minutes=15,
            expected_vol_pct=features.realized_vol_30min,
        )


def _sma(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _bb_avg(history: list[FeatureBundle]) -> float:
    widths = [b.bb_width for b in history if b.bb_width > 0]
    return sum(widths) / len(widths) if widths else 0.0
