"""Opening Range Breakout (ORB) primitive.

Range = (low, high) over the first 30 min of the session.
Long signal when LTP breaks the high with volume > 1.5× average.
Short signal when LTP breaks the low with volume > 1.5× average.
Strength = (LTP - high) / (high - low), clamped to [-1, 1].
"""
from __future__ import annotations

import math

from src.quant.feature_store import FeatureBundle
from src.quant.primitives.base import BasePrimitive, Signal

_VOLUME_MULTIPLIER = 1.5
_WARMUP_BARS = 10  # 30 min / 3 min per bar = 10 bars


class ORBPrimitive(BasePrimitive):
    """Opening Range Breakout."""

    name = "orb"
    warmup_bars = _WARMUP_BARS

    def compute_signal(
        self,
        features: FeatureBundle,
        history: list[FeatureBundle],
        *,
        trace: dict | None = None,
    ) -> Signal | None:
        if not self._past_warmup(history):
            return None
        if features.orb_high is None or features.orb_low is None:
            return None

        orb_high = features.orb_high
        orb_low = features.orb_low
        orb_range = orb_high - orb_low
        if orb_range <= 0:
            return None

        ltp = features.underlying_ltp
        vol = features.underlying_volume_3min

        avg_vol = self._average_volume(history)
        vol_ok = avg_vol == 0 or vol > _VOLUME_MULTIPLIER * avg_vol

        bullish = ltp > orb_high and vol_ok
        bearish = ltp < orb_low and vol_ok
        raw = 0.0
        strength = 0.0
        if bullish:
            raw = (ltp - orb_high) / orb_range
            strength = self._clamp(raw)
        elif bearish:
            raw = (orb_low - ltp) / orb_range
            strength = self._clamp(raw)

        # Only populate the trace when we actually return a Signal — keeps
        # the schema invariant that primitive_trace mirrors row presence.
        if trace is not None and (bullish or bearish):
            trace["name"] = self.name
            trace["inputs"] = {
                "ltp": float(ltp),
                "orb_high": float(orb_high),
                "orb_low": float(orb_low),
                "vol_3min": float(vol),
                "avg_vol": float(avg_vol),
            }
            trace["intermediates"] = {
                "orb_range": float(orb_range),
                "vol_multiplier_satisfied": bool(vol_ok),
                "raw": float(raw),
            }
            ref = orb_high if bullish else orb_low
            trace["formula"] = (
                f"strength = clamp((|ltp − {'high' if bullish else 'low'}|) / range) "
                f"= clamp(|{ltp:.2f} − {ref:.2f}| / {orb_range:.2f}) = {strength:.4f}"
            )

        if bullish:
            return Signal(
                direction="bullish",
                strength=strength,
                strategy_class="long_call",
                expected_horizon_minutes=30,
                expected_vol_pct=features.realized_vol_30min,
            )
        if bearish:
            return Signal(
                direction="bearish",
                strength=strength,
                strategy_class="long_put",
                expected_horizon_minutes=30,
                expected_vol_pct=features.realized_vol_30min,
            )
        return None

    @staticmethod
    def _average_volume(history: list[FeatureBundle]) -> float:
        vols = [b.underlying_volume_3min for b in history if b.underlying_volume_3min > 0]
        return sum(vols) / len(vols) if vols else 0.0
