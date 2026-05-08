"""VWAP Z-score mean-reversion primitive.

Z = (LTP - VWAP) / σ_price
where σ_price is the price-level standard deviation of recent LTPs (in ₹), so
the numerator and denominator share units. Annualised return-σ would be
unitless and would mix scales — see review notes for the bug it caused.

Signal: short if Z > 2.0, long if Z < -2.0.
Strength = tanh(|Z| - 2). Skips during strong trending regime (ADX proxy).
"""
from __future__ import annotations

import math

from src.quant.feature_store import FeatureBundle
from src.quant.primitives.base import BasePrimitive, Signal

_Z_THRESHOLD = 2.0
_WARMUP_BARS = 10


class VWAPRevertPrimitive(BasePrimitive):
    """VWAP Z-score mean reversion."""

    name = "vwap_revert"
    warmup_minutes = _WARMUP_BARS

    def compute_signal(
        self,
        features: FeatureBundle,
        history: list[FeatureBundle],
    ) -> Signal | None:
        if not self._past_warmup(history):
            return None

        # Price-level σ (₹) over the warmup window. This is the correct
        # denominator for a price-distance Z-score.
        ltps = [b.underlying_ltp for b in history if b.underlying_ltp > 0]
        if len(ltps) < 5:
            return None
        n = len(ltps)
        mean = sum(ltps) / n
        var = sum((p - mean) ** 2 for p in ltps) / max(n - 1, 1)
        price_std = math.sqrt(var)
        if price_std <= 0:
            return None

        z = (features.underlying_ltp - features.vwap_today) / price_std
        rv = features.realized_vol_30min  # passed through for trail-stop sizing

        # Trending-regime gate: approximate ADX via recent directional movement.
        if self._is_trending(history):
            return None

        if z > _Z_THRESHOLD:
            strength = self._clamp(self._tanh_strength(abs(z) - _Z_THRESHOLD))
            return Signal(
                direction="bearish",
                strength=strength,
                strategy_class="long_put",
                expected_horizon_minutes=15,
                expected_vol_pct=rv,
            )

        if z < -_Z_THRESHOLD:
            strength = self._clamp(self._tanh_strength(abs(z) - _Z_THRESHOLD))
            return Signal(
                direction="bullish",
                strength=strength,
                strategy_class="long_call",
                expected_horizon_minutes=15,
                expected_vol_pct=rv,
            )

        return None

    @staticmethod
    def _is_trending(history: list[FeatureBundle]) -> bool:
        """Lightweight ADX proxy: consecutive directional bars > 60% of window."""
        if len(history) < 5:
            return False
        ltps = [b.underlying_ltp for b in history[-10:]]
        if len(ltps) < 2:
            return False
        ups = sum(1 for i in range(1, len(ltps)) if ltps[i] > ltps[i - 1])
        ratio = ups / (len(ltps) - 1)
        # > 75% up or < 25% up ≈ trending
        return ratio > 0.75 or ratio < 0.25
