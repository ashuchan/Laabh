"""VWAP-anchored Z-score mean-reversion primitive.

Z-score formulation (rewritten in Phase 2 of the bug audit):

    expected_displacement = realized_vol_30min · sqrt(H_min / B_year_1min) · LTP
    z = (LTP − VWAP) / expected_displacement
    z_clipped = clamp(z, ±_Z_CAP)

Where:
  * ``realized_vol_30min`` is the annualised σ of recent 1-min log-returns
    (already in the FeatureBundle).
  * ``H_min`` is the displacement horizon in minutes — the natural scale
    over which we ask "is this an unusual deviation?". 30 min matches the
    primitive's warmup window.
  * ``B_year_1min`` = 94,500 (252 trading days × 375 1-min bars/day) — the
    same constant the backtest feature store uses to annualise.
  * ``_Z_CAP`` is a defensive cap that prevents tail outliers from
    saturating the strength formula and monopolising the bandit.

Why this matters (the bug it replaces):
    The previous denominator was ``std of recent LTPs around their own mean``.
    When prices stabilised at a new level (low recent variance) but had drifted
    far from session VWAP, that std collapsed → ``z`` exploded. Live-data
    smoke showed ``|z|`` up to 31.9, with a median of 5.55 — neither remotely
    consistent with a Gaussian Z-score. After this rewrite, ``|z|`` is bounded
    by ``_Z_CAP`` and the displacement is normalised against a properly-scaled
    annualised vol estimate, so the strength signal actually means something.

Signal direction:
    z > +_Z_THRESHOLD → price stretched ABOVE VWAP → bearish (bet on revert down)
    z < −_Z_THRESHOLD → price stretched BELOW VWAP → bullish (bet on revert up)
"""
from __future__ import annotations

import math

from src.quant.feature_store import FeatureBundle
from src.quant.primitives.base import BasePrimitive, Signal

_Z_THRESHOLD = 2.0
_Z_CAP = 4.0
# Take-profit threshold — when |z| drops below this we consider the
# reversion "done" and exit the position. Set at 1.0 (half-way back to
# VWAP from the most aggressive z=2 entry) — captures the meat of the
# reversion before the trailing stop tightens enough to fire on noise.
# The original 0.5 proved too tight in smoke testing: trades that *did*
# revert profitably hit their trailing stop before z came within 0.5.
_TAKE_PROFIT_Z = 1.0
_WARMUP_BARS = 10
# Horizon over which we measure "expected" price displacement (minutes).
# Matches the primitive's 10-bar × 3-min warmup window.
_DISPLACEMENT_HORIZON_MIN = 30
# Same annualisation constant as ``BacktestFeatureStore`` so the units of
# ``realized_vol_30min`` and our denominator agree.
_BARS_PER_YEAR_1MIN = 94_500


def _compute_capped_z(features: FeatureBundle) -> tuple[float, float, float] | None:
    """Compute (raw_z, capped_z, expected_displacement) for a FeatureBundle.

    Returns None when the inputs can't produce a meaningful z (zero vol or
    zero displacement). Shared by ``compute_signal`` and
    ``should_take_profit`` so the entry and exit decisions use exactly the
    same math — there's no scenario where they could disagree on what z is.
    """
    rv = features.realized_vol_30min
    if rv <= 0:
        return None
    per_1min_log_sigma = rv / math.sqrt(_BARS_PER_YEAR_1MIN)
    horizon_log_sigma = per_1min_log_sigma * math.sqrt(_DISPLACEMENT_HORIZON_MIN)
    expected_displacement = horizon_log_sigma * features.underlying_ltp
    if expected_displacement <= 0:
        return None
    raw_z = (features.underlying_ltp - features.vwap_today) / expected_displacement
    capped_z = max(-_Z_CAP, min(_Z_CAP, raw_z))
    return raw_z, capped_z, expected_displacement


class VWAPRevertPrimitive(BasePrimitive):
    """VWAP-anchored Z-score mean reversion."""

    name = "vwap_revert"
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

        # Trending-regime gate (lightweight ADX proxy) — skip when the
        # market is in a strong directional move; reverting against a trend
        # is what made earlier days bleed.
        if self._is_trending(history):
            return None

        zs = _compute_capped_z(features)
        if zs is None:
            return None
        raw_z, z, expected_displacement = zs
        rv = features.realized_vol_30min  # passed through for trail-stop sizing

        fired = abs(z) > _Z_THRESHOLD
        strength = (
            self._clamp(self._tanh_strength(abs(z) - _Z_THRESHOLD)) if fired else 0.0
        )

        if trace is not None and fired:
            trace["name"] = self.name
            trace["inputs"] = {
                "ltp": float(features.underlying_ltp),
                "vwap_today": float(features.vwap_today),
                "rv_30min": float(rv),
            }
            trace["intermediates"] = {
                "expected_displacement": float(expected_displacement),
                "raw_z": float(raw_z),
                "z_capped": float(z),
                "z_threshold": float(_Z_THRESHOLD),
                "z_cap": float(_Z_CAP),
            }
            trace["formula"] = (
                f"σ_disp = rv × √(H/B) × ltp = {rv:.4f} × "
                f"√({_DISPLACEMENT_HORIZON_MIN}/{_BARS_PER_YEAR_1MIN}) × "
                f"{features.underlying_ltp:.2f} = {expected_displacement:.4f}; "
                f"z = (ltp − vwap) / σ_disp = {raw_z:+.4f} "
                f"→ capped {z:+.4f}; "
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

    def should_take_profit(self, position, current_features: FeatureBundle) -> bool:
        """Close when price has reverted close to VWAP (|z| < take-profit).

        The vwap_revert hypothesis is "price will revert to VWAP" — when
        that happens, we got what we came for. The generic ``should_close``
        in exits.py only knows about stops + signal flips and would
        otherwise hold the position until a trailing stop or time stop
        fires, which on a fast reversion can miss the window entirely.
        """
        zs = _compute_capped_z(current_features)
        if zs is None:
            return False
        _raw_z, capped_z, _disp = zs
        return abs(capped_z) < _TAKE_PROFIT_Z

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
