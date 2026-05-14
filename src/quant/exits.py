"""Layer 4 — Per-position exit rules.

Rules (in priority order):
 1. Vol-adjusted trailing stop:
       stop = peak_premium - 2.5 × σ_per_bar × sqrt(holding_bars) × entry_premium
       where σ_per_bar = annualised_vol / sqrt(26040)  (26040 = 252d × 103.33 bars/d)
 2. Profit ratchet:
       at +1R: move stop to breakeven
       at +2R: trail at 1R from peak
 3. Adverse-signal flip: same arm's signal flips with |strength| > 0.6 → close
 4. Time stop: hard_exit_time (14:30 IST by default; configurable per call)
"""
from __future__ import annotations

import math
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from decimal import Decimal
from typing import Literal

import pytz

_IST = pytz.timezone("Asia/Kolkata")
_BARS_PER_YEAR = 26040  # 252 trading days × ~103.33 3-min bars per day


@dataclass
class OpenPosition:
    """State of a currently-open quant trade."""

    arm_id: str
    underlying_id: str
    direction: Literal["bullish", "bearish"]
    entry_premium_net: Decimal
    entry_at: datetime
    lots: int = 1
    trade_id: uuid.UUID | None = None   # DB row id, set by orchestrator after flush
    # The exact LinTS context vector seen by the selector at open time. Read
    # at close to pass to ``selector.update`` so the bandit learns against
    # the same context it sampled from (review fix P0 #1).
    entry_context: "np.ndarray | None" = None
    peak_premium: Decimal = field(init=False)
    initial_risk_r: Decimal = Decimal("0")   # set by orchestrator after open

    def __post_init__(self) -> None:
        self.peak_premium = self.entry_premium_net


def should_close(
    position: OpenPosition,
    current_premium: Decimal,
    realized_vol_3min_annualised: float,
    current_time: datetime,
    current_signals: list,
    *,
    hard_exit_time: time = time(14, 30),
    as_of: datetime | None = None,
    dryrun_run_id=None,
) -> tuple[bool, str]:
    """Return (close_now, reason) for the given open position.

    Args:
        position: The open position being evaluated.
        current_premium: Current mid premium of the position's legs.
        realized_vol_3min_annualised: Annualised realized vol from feature_store.
        current_time: UTC now (tz-aware or naive — treated as UTC).
        current_signals: Signals emitted this tick [(arm_id, Signal)].
        hard_exit_time: Force-close at this IST time (default 14:30).
    """
    # Normalise to tz-aware UTC
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)

    # Refresh peak
    if current_premium > position.peak_premium:
        position.peak_premium = current_premium

    holding_minutes = (
        current_time
        - (position.entry_at if position.entry_at.tzinfo else position.entry_at.replace(tzinfo=timezone.utc))
    ).total_seconds() / 60.0

    # 1. Time stop — check in IST
    if current_time.astimezone(_IST).time() >= hard_exit_time:
        return True, "time_stop"

    # 2. Vol-adjusted trailing stop
    vol_stop = _trailing_stop(position, realized_vol_3min_annualised, holding_minutes)
    if current_premium <= vol_stop:
        return True, "trailing_stop"

    # 3. Profit ratchet
    r = position.initial_risk_r
    if r > 0:
        pnl = current_premium - position.entry_premium_net
        if pnl >= 2 * r:
            # Trail at 1R from peak
            if current_premium <= position.peak_premium - r:
                return True, "trailing_stop"
        elif pnl >= r:
            # Move stop to breakeven
            if current_premium <= position.entry_premium_net:
                return True, "trailing_stop"

    # 4. Adverse-signal flip
    for arm_id, signal in current_signals:
        if arm_id == position.arm_id and signal is not None:
            opposite = (
                signal.direction == "bearish" and position.direction == "bullish"
            ) or (
                signal.direction == "bullish" and position.direction == "bearish"
            )
            if opposite and signal.strength > 0.6:
                return True, "signal_flip"

    return False, ""


def _trailing_stop(
    position: OpenPosition,
    realized_vol_3min_annualised: float,
    holding_minutes: float,
) -> Decimal:
    """Compute the current trailing stop premium level.

    Converts annualised vol → per-3min-bar fraction, then scales by
    sqrt(holding_bars) and the entry premium to get a ₹ trail.
    """
    per_bar_sigma = realized_vol_3min_annualised / math.sqrt(_BARS_PER_YEAR)
    holding_bars = max(holding_minutes / 3.0, 1.0)
    trail_fraction = 2.5 * per_bar_sigma * math.sqrt(holding_bars)
    trail = Decimal(str(trail_fraction)) * position.entry_premium_net
    return position.peak_premium - trail
