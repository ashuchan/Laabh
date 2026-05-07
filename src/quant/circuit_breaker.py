"""Layer 5 — Day-level circuit breaker.

Rules:
 - Lock-in:   nav_pct >= LOCKIN_TARGET_PCT → sizer halves f_max (flag only here)
 - Kill:      nav_pct <= -KILL_SWITCH_DD_PCT → no new entries
 - Cool-off:  ≥ COOLOFF_CONSECUTIVE_LOSSES losses on one arm → skip arm for N min
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from src.config import get_settings


@dataclass
class CircuitState:
    """Mutable day-level state shared by sizer and orchestrator."""

    starting_nav: float
    lockin_active: bool = False
    kill_active: bool = False
    lockin_fired_at: datetime | None = None
    kill_fired_at: datetime | None = None
    # arm_id → (consecutive losses, cooloff_until)
    _arm_consecutive_losses: dict[str, int] = field(default_factory=dict)
    _arm_cooloff_until: dict[str, datetime] = field(default_factory=dict)

    def check_and_fire(self, current_nav: float, now: datetime) -> None:
        """Evaluate NAV-based rules and update flags (idempotent)."""
        settings = get_settings()
        nav_pct = (current_nav - self.starting_nav) / self.starting_nav

        if not self.lockin_active and nav_pct >= settings.laabh_quant_lockin_target_pct:
            self.lockin_active = True
            self.lockin_fired_at = now

        if not self.kill_active and nav_pct <= -settings.laabh_quant_kill_switch_dd_pct:
            self.kill_active = True
            self.kill_fired_at = now

    def record_loss(self, arm_id: str, now: datetime) -> None:
        """Increment loss counter for *arm_id* and set cooloff if threshold hit."""
        settings = get_settings()
        count = self._arm_consecutive_losses.get(arm_id, 0) + 1
        self._arm_consecutive_losses[arm_id] = count
        if count >= settings.laabh_quant_cooloff_consecutive_losses:
            until = now + timedelta(minutes=settings.laabh_quant_cooloff_minutes)
            self._arm_cooloff_until[arm_id] = until
            self._arm_consecutive_losses[arm_id] = 0

    def record_win(self, arm_id: str) -> None:
        """Reset consecutive-loss counter for *arm_id* on a winning trade."""
        self._arm_consecutive_losses[arm_id] = 0

    def arm_in_cooloff(self, arm_id: str, now: datetime) -> bool:
        """Return True if *arm_id* is currently in its cool-off window."""
        until = self._arm_cooloff_until.get(arm_id)
        if until is None:
            return False
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        until_utc = until.replace(tzinfo=timezone.utc) if until.tzinfo is None else until
        return now < until_utc
