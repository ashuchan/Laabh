"""Layer 5 — Day-level circuit breaker.

Rules:
 - Lock-in:   nav_pct >= lockin_target_pct → sizer halves f_max (flag only here)
 - Kill:      nav_pct <= -kill_switch_dd_pct → no new entries
 - Cool-off:  ≥ cooloff_consecutive_losses losses on one arm → skip arm for N min
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


@dataclass
class CircuitState:
    """Mutable day-level state shared by sizer and orchestrator."""

    starting_nav: float
    lockin_target_pct: float = 0.05
    kill_switch_dd_pct: float = 0.03
    cooloff_consecutive_losses: int = 3
    cooloff_minutes: int = 30

    lockin_active: bool = False
    kill_active: bool = False
    lockin_fired_at: datetime | None = None
    kill_fired_at: datetime | None = None
    # arm_id → (consecutive losses, cooloff_until)
    _arm_consecutive_losses: dict[str, int] = field(default_factory=dict)
    _arm_cooloff_until: dict[str, datetime] = field(default_factory=dict)

    def check_and_fire(self, current_nav: float, now: datetime) -> None:
        """Evaluate NAV-based rules and update flags (idempotent)."""
        if self.starting_nav == 0:
            return
        nav_pct = (current_nav - self.starting_nav) / self.starting_nav

        if not self.lockin_active and nav_pct >= self.lockin_target_pct:
            self.lockin_active = True
            self.lockin_fired_at = now

        if not self.kill_active and nav_pct <= -self.kill_switch_dd_pct:
            self.kill_active = True
            self.kill_fired_at = now

    def record_loss(self, arm_id: str, now: datetime) -> None:
        """Increment loss counter for *arm_id* and set cooloff if threshold hit."""
        count = self._arm_consecutive_losses.get(arm_id, 0) + 1
        self._arm_consecutive_losses[arm_id] = count
        if count >= self.cooloff_consecutive_losses:
            until = now + timedelta(minutes=self.cooloff_minutes)
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
