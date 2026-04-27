"""Intraday position manager — Phase 4 real-time trade lifecycle.

Responsibilities during the trading session (09:15-15:30 IST):
  - Entry gating: no entries in first N minutes (pre-market noise), no entries after hard_exit_time
  - Position tracking: open positions with entry price, peak price, stop, target
  - Stop-loss monitoring: exit if current premium ≤ stop_level
  - Trailing stop: update stop when position is up > scale_out_pct from entry
  - Hard exit: force-close all positions at hard_exit_time (default 14:30)
  - Cooldown: block new entries on an instrument for N minutes after a stop-loss
  - Max concurrent positions: cap open positions at fno_phase4_max_open_positions

Pure logic lives in stateless functions; the IntradayState dataclass holds
mutable position state. No DB I/O in this module — the orchestrator persists state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from decimal import Decimal
from typing import Literal


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class OpenPosition:
    instrument_id: str
    symbol: str
    strategy_name: str
    option_type: Literal["CE", "PE"]
    strike: Decimal
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    lots: int
    lot_size: int
    peak_price: Decimal = field(init=False)
    trailing_active: bool = False
    entered_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))

    def __post_init__(self) -> None:
        self.peak_price = self.entry_price


@dataclass
class IntradayState:
    """Mutable intraday session state — one per trading day."""
    open_positions: list[OpenPosition] = field(default_factory=list)
    cooldowns: dict[str, datetime] = field(default_factory=dict)   # instrument_id → cooldown_until
    hard_exited: bool = False


# ---------------------------------------------------------------------------
# Pure gating and lifecycle helpers
# ---------------------------------------------------------------------------

def is_entry_allowed(
    now: datetime,
    instrument_id: str,
    state: IntradayState,
    *,
    market_open: time = time(9, 15),
    no_entry_minutes: int = 30,
    hard_exit_time: time = time(14, 30),
    max_open_positions: int = 3,
) -> tuple[bool, str]:
    """Check if a new entry is allowed at this moment.

    Returns (allowed, reason_if_not).
    """
    now_time = now.time().replace(tzinfo=None)

    if state.hard_exited:
        return False, "hard_exit_triggered"

    gate_open = time(
        market_open.hour + (market_open.minute + no_entry_minutes) // 60,
        (market_open.minute + no_entry_minutes) % 60,
    )
    if now_time < gate_open:
        return False, f"pre_market_gate_{no_entry_minutes}min"

    if now_time >= hard_exit_time:
        return False, "past_hard_exit_time"

    if len(state.open_positions) >= max_open_positions:
        return False, f"max_positions_{max_open_positions}"

    cooldown_until = state.cooldowns.get(instrument_id)
    if cooldown_until and now < cooldown_until:
        return False, f"cooldown_until_{cooldown_until.isoformat()}"

    return True, ""


def check_stop_loss(position: OpenPosition, current_price: Decimal) -> bool:
    """Return True if the position should be stopped out."""
    return current_price <= position.stop_price


def check_target(position: OpenPosition, current_price: Decimal) -> bool:
    """Return True if the position has hit its profit target."""
    return current_price >= position.target_price


def update_trailing_stop(
    position: OpenPosition,
    current_price: Decimal,
    *,
    scale_out_pct: float = 0.30,
    trailing_stop_pct: float = 0.20,
) -> bool:
    """Update trailing stop if price has moved significantly in our favour.

    Returns True if stop was moved (trailing activated or updated).
    """
    gain_pct = (current_price - position.entry_price) / position.entry_price

    if gain_pct >= Decimal(str(scale_out_pct)):
        if current_price > position.peak_price:
            position.peak_price = current_price
            position.trailing_active = True
            new_stop = position.peak_price * (1 - Decimal(str(trailing_stop_pct)))
            if new_stop > position.stop_price:
                position.stop_price = new_stop.quantize(Decimal("0.01"))
                return True
    return False


def should_hard_exit(now: datetime, hard_exit_time: time = time(14, 30)) -> bool:
    """Return True if it's time for the hard intraday exit."""
    return now.time().replace(tzinfo=None) >= hard_exit_time


def apply_tick(
    position: OpenPosition,
    current_price: Decimal,
    *,
    scale_out_pct: float = 0.30,
    trailing_stop_pct: float = 0.20,
) -> Literal["hold", "stop", "target"]:
    """Process a price tick for an open position. Returns the action to take."""
    update_trailing_stop(
        position, current_price,
        scale_out_pct=scale_out_pct,
        trailing_stop_pct=trailing_stop_pct,
    )
    if check_stop_loss(position, current_price):
        return "stop"
    if check_target(position, current_price):
        return "target"
    return "hold"
