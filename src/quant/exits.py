"""Layer 4 — Per-position exit rules.

Rules (in priority order):
 1. Vol-adjusted trailing stop:
       stop = peak_premium - 2.5 × realized_vol_3min × sqrt(holding_minutes)
 2. Profit ratchet:
       at +1R: move stop to breakeven
       at +2R: trail at 1R from peak
 3. Adverse-signal flip: same arm's signal flips with |strength| > 0.6 → close
 4. Time stop: QUANT_HARD_EXIT_TIME (14:30 IST)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from src.config import get_settings


@dataclass
class OpenPosition:
    """State of a currently-open quant trade."""

    arm_id: str
    underlying_id: str
    direction: Literal["bullish", "bearish"]
    entry_premium_net: Decimal
    entry_at: datetime
    peak_premium: Decimal = field(init=False)
    initial_risk_r: Decimal = Decimal("0")   # cost of entry — set by orchestrator

    def __post_init__(self) -> None:
        self.peak_premium = self.entry_premium_net


def should_close(
    position: OpenPosition,
    current_premium: Decimal,
    realized_vol_3min: float,
    current_time: datetime,
    current_signals: list,        # list[tuple[arm_id, Signal]]
    *,
    as_of: datetime | None = None,
    dryrun_run_id=None,
) -> tuple[bool, str]:
    """Return (close_now, reason) for the given open position.

    Args:
        position: The open position being evaluated.
        current_premium: Current mid premium of the position's legs.
        realized_vol_3min: Annualised 3-min realized vol of the underlying.
        current_time: UTC now.
        current_signals: Signals emitted this tick [(arm_id, Signal)].
    """
    settings = get_settings()

    # Refresh peak
    if current_premium > position.peak_premium:
        position.peak_premium = current_premium

    holding_minutes = (
        current_time.replace(tzinfo=timezone.utc)
        - position.entry_at.replace(tzinfo=timezone.utc)
    ).total_seconds() / 60.0

    # 1. Time stop
    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    exit_time = settings.laabh_quant_hard_exit_time
    current_ist = current_time.astimezone(ist).time()
    if current_ist >= exit_time:
        return True, "time_stop"

    # 2. Vol-adjusted trailing stop
    vol_stop = _trailing_stop(position, realized_vol_3min, holding_minutes)
    if current_premium <= vol_stop:
        return True, "trailing_stop"

    # 3. Profit ratchet
    r = position.initial_risk_r
    if r > 0:
        pnl = current_premium - position.entry_premium_net
        if pnl >= 2 * r:
            # Trail at 1R from peak
            ratchet_stop = position.peak_premium - r
            if current_premium <= ratchet_stop:
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
    realized_vol_3min: float,
    holding_minutes: float,
) -> Decimal:
    """Compute the current trailing stop premium level."""
    sigma = realized_vol_3min
    trail = Decimal(str(2.5 * sigma * math.sqrt(max(holding_minutes, 1))))
    return position.peak_premium - trail
