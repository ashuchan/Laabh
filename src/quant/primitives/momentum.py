"""Volume-weighted momentum primitive.

mom = sum(log_returns over last n=10 bars) × volume_weight
Signal direction = sign(mom).
Strength = tanh(2 × mom / realized_vol_30min).
"""
from __future__ import annotations

import math

from src.quant.feature_store import FeatureBundle
from src.quant.primitives.base import BasePrimitive, Signal

_N_BARS = 10
_WARMUP_BARS = _N_BARS + 1  # need one extra to compute the first log-return


class MomentumPrimitive(BasePrimitive):
    """Volume-weighted n-bar momentum."""

    name = "momentum"
    warmup_minutes = _WARMUP_BARS

    def compute_signal(
        self,
        features: FeatureBundle,
        history: list[FeatureBundle],
    ) -> Signal | None:
        if not self._past_warmup(history):
            return None

        rv = features.realized_vol_30min
        if rv <= 0:
            return None

        window = history[-_N_BARS:]
        ltps = [b.underlying_ltp for b in window] + [features.underlying_ltp]
        vols = [b.underlying_volume_3min for b in window] + [features.underlying_volume_3min]

        total_vol = sum(vols)
        if total_vol == 0:
            return None

        weighted_mom = 0.0
        for i in range(1, len(ltps)):
            if ltps[i - 1] <= 0 or ltps[i] <= 0:
                continue
            log_ret = math.log(ltps[i] / ltps[i - 1])
            w = vols[i] / total_vol
            weighted_mom += log_ret * w

        if weighted_mom == 0:
            return None

        strength = self._clamp(self._tanh_strength(2.0 * weighted_mom / rv))

        if weighted_mom > 0:
            return Signal(
                direction="bullish",
                strength=strength,
                strategy_class="long_call",
                expected_horizon_minutes=15,
                expected_vol_pct=rv,
            )
        return Signal(
            direction="bearish",
            strength=strength,
            strategy_class="long_put",
            expected_horizon_minutes=15,
            expected_vol_pct=rv,
        )
