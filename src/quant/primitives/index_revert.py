"""Index-constituent basket reversion primitive.

Only fires for index underlyings (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY).
Z = (INDEX_LTP - basket × scaling) / basket_std_30d
Signal: short if Z > 2, long if Z < -2.
Requires features.constituent_basket_value to be populated.

KNOWN LIMITATION: the scaling factor is approximated by
    scaling = index_ltp[oldest] / basket[oldest]
which drifts as constituent weights change. A proper offline-calibrated
scaling (eg. from yesterday's close) should replace this before the
primitive carries meaningful production weight.
"""
from __future__ import annotations

import math

from src.quant.feature_store import FeatureBundle
from src.quant.primitives.base import BasePrimitive, Signal

_INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"}
_Z_THRESHOLD = 2.0
_WARMUP_BARS = 10


class IndexRevertPrimitive(BasePrimitive):
    """Index vs constituent basket reversion."""

    name = "index_revert"
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

        # Only apply to index underlyings
        symbol = features.underlying_symbol.upper()
        if not any(symbol.startswith(idx) for idx in _INDEX_SYMBOLS):
            return None

        basket = features.constituent_basket_value
        if basket is None or basket <= 0:
            return None

        # Scaling: ratio of index LTP to basket at the oldest history bar
        # (approximation; a proper scaling factor should be calibrated offline)
        index_ltps = [b.underlying_ltp for b in history if b.underlying_ltp > 0]
        basket_vals = [b.constituent_basket_value for b in history if b.constituent_basket_value]
        if not index_ltps or not basket_vals:
            return None

        scaling = index_ltps[0] / basket_vals[0] if basket_vals[0] else 1.0
        scaled_basket = basket * scaling

        # 30-day basket std approximated from history
        basket_std = _std(basket_vals)
        if basket_std <= 0:
            return None

        z = (features.underlying_ltp - scaled_basket) / basket_std
        rv = features.realized_vol_30min

        fired = abs(z) > _Z_THRESHOLD
        strength = (
            self._clamp(self._tanh_strength(abs(z) - _Z_THRESHOLD)) if fired else 0.0
        )

        if trace is not None and fired:
            trace["name"] = self.name
            trace["inputs"] = {
                "ltp": float(features.underlying_ltp),
                "basket": float(basket),
                "rv_30min": float(rv),
            }
            trace["intermediates"] = {
                "scaling": float(scaling),
                "scaled_basket": float(scaled_basket),
                "basket_std": float(basket_std),
                "z": float(z),
                "z_threshold": float(_Z_THRESHOLD),
            }
            trace["formula"] = (
                f"z = (ltp − scaled_basket) / σ = "
                f"({features.underlying_ltp:.2f} − {scaled_basket:.2f}) / "
                f"{basket_std:.4f} = {z:.4f}; "
                f"strength = tanh(|z| − {_Z_THRESHOLD}) = {strength:.4f}"
            )

        if z > _Z_THRESHOLD:
            return Signal(
                direction="bearish",
                strength=strength,
                strategy_class="long_put",
                expected_horizon_minutes=15,
                expected_vol_pct=rv,
            )
        if z < -_Z_THRESHOLD:
            return Signal(
                direction="bullish",
                strength=strength,
                strategy_class="long_call",
                expected_horizon_minutes=15,
                expected_vol_pct=rv,
            )
        return None


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    n = len(values)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(var)
