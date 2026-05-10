"""Order Flow Imbalance (OFI) primitive.

Based on Cont-Kukanov-Stoikov (2014).
OFI_t = bid_qty_change_3min - ask_qty_change_3min
Signal direction = sign(OFI).
Strength = tanh(OFI / EMA_OFI_20).
Skips if ATM bid/ask is stale (both zero).
"""
from __future__ import annotations

import math

from src.quant.feature_store import FeatureBundle
from src.quant.primitives.base import BasePrimitive, Signal

_WARMUP_BARS = 5
_EMA_ALPHA = 2 / (20 + 1)  # 20-period EMA


class OFIPrimitive(BasePrimitive):
    """Order Flow Imbalance."""

    name = "ofi"
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

        # Stale-quote gate
        if float(features.atm_bid) == 0 and float(features.atm_ask) == 0:
            return None

        ofi = features.bid_volume_3min_change - features.ask_volume_3min_change

        ema_ofi = self._ema_ofi(history)
        if ema_ofi == 0:
            # Cold-start: use absolute OFI as proxy
            ema_ofi = abs(ofi) or 1.0

        strength = self._clamp(self._tanh_strength(ofi / ema_ofi))
        if strength == 0:
            return None

        direction: str
        strategy: str
        if ofi > 0:
            direction, strategy = "bullish", "long_call"
        else:
            direction, strategy = "bearish", "long_put"

        if trace is not None:
            trace["name"] = self.name
            trace["inputs"] = {
                "bid_volume_3min_change": float(features.bid_volume_3min_change),
                "ask_volume_3min_change": float(features.ask_volume_3min_change),
                "atm_bid": float(features.atm_bid),
                "atm_ask": float(features.atm_ask),
            }
            trace["intermediates"] = {
                "ofi": float(ofi),
                "ema_ofi": float(ema_ofi),
            }
            trace["formula"] = (
                f"ofi = bid_Δ − ask_Δ = {ofi:.2f}; "
                f"strength = tanh(ofi / ema_ofi) = tanh({ofi:.2f} / {ema_ofi:.2f}) "
                f"= {strength:.4f}"
            )

        return Signal(
            direction=direction,  # type: ignore[arg-type]
            strength=abs(strength),
            strategy_class=strategy,
            expected_horizon_minutes=9,
            expected_vol_pct=features.realized_vol_30min,
        )

    @staticmethod
    def _ema_ofi(history: list[FeatureBundle]) -> float:
        """Exponential moving average of OFI over history."""
        ema = 0.0
        for b in history:
            ofi = b.bid_volume_3min_change - b.ask_volume_3min_change
            ema = _EMA_ALPHA * ofi + (1 - _EMA_ALPHA) * ema
        return abs(ema) if ema != 0 else 1.0
