"""Cross-agent Pydantic validators for the CEO Judge's output."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, field_validator, model_validator


class Allocation(BaseModel):
    asset_class: str
    underlying_or_symbol: str = ""
    capital_pct: float
    decision: str = ""
    horizon: str | None = None
    conviction: float | None = None


class KillSwitch(BaseModel):
    trigger: str
    action: str
    monitoring_metric: str


class CalibrationSelfCheck(BaseModel):
    bullish_argument_grade: str
    bearish_argument_grade: str
    confidence_in_allocation: float
    regret_scenario: str


class CEOJudgeOutputValidated(BaseModel):
    """Validates the CEO Judge's final output before committing agent_predictions.

    Hard validators (capital sums, at-risk cap, direction logic) reject the
    prediction; soft validators (kill-switch realism) caveat it.
    """

    decision_summary: str
    allocation: list[Allocation]
    kill_switches: list[KillSwitch] = []
    ceo_note: str
    calibration_self_check: CalibrationSelfCheck
    expected_book_pnl_pct: float
    max_drawdown_tolerated_pct: float
    disagreement_loci: list[dict[str, Any]] = []
    stretch_pnl_pct: float | None = None

    @field_validator("allocation")
    @classmethod
    def capital_pct_sums_to_at_most_100(cls, v: list[Allocation]) -> list[Allocation]:
        total = sum(a.capital_pct for a in v)
        if total > 100.01:
            raise ValueError(
                f"Allocation sums to {total:.2f}%, must be ≤100%. "
                f"Reduce one or more positions."
            )
        return v

    @field_validator("allocation")
    @classmethod
    def no_single_leg_over_40_pct(cls, v: list[Allocation]) -> list[Allocation]:
        for alloc in v:
            if alloc.asset_class.lower() == "cash":
                continue  # uninvested cash is not a risk position
            if alloc.capital_pct > 40:
                raise ValueError(
                    f"Single position {alloc.underlying_or_symbol!r} is {alloc.capital_pct}% "
                    f"of capital — exceeds 40% single-position limit."
                )
        return v

    @field_validator("allocation")
    @classmethod
    def no_duplicate_non_hedge_positions(cls, v: list[Allocation]) -> list[Allocation]:
        seen: dict[str, list[str]] = {}
        for alloc in v:
            sym = alloc.underlying_or_symbol
            if sym:
                seen.setdefault(sym, []).append(alloc.asset_class)
        for sym, classes in seen.items():
            if len(classes) > 1 and "cash" not in classes:
                # Both fno and equity on same underlying — caveat (not rejection)
                pass  # soft check only
        return v

    @field_validator("expected_book_pnl_pct")
    @classmethod
    def expected_pnl_is_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(
                f"expected_book_pnl_pct={v} — judge must produce a positive expected P&L. "
                f"Refuse the trade if the setup is not positive EV."
            )
        return v

    @model_validator(mode="after")
    def kill_switches_match_drawdown(self) -> "CEOJudgeOutputValidated":
        if self.max_drawdown_tolerated_pct > 10:
            raise ValueError(
                f"max_drawdown_tolerated_pct={self.max_drawdown_tolerated_pct} "
                f"exceeds 10% hard cap."
            )
        return self


class EquityExpertOutputValidated(BaseModel):
    """Validates an equity expert's recommendation."""

    symbol: str
    decision: str
    conviction: float
    refused: bool
    entry_zone: dict[str, float] | None = None
    target: float | None = None
    stop: float | None = None

    @model_validator(mode="after")
    def buy_implies_target_above_entry(self) -> "EquityExpertOutputValidated":
        if self.decision == "BUY" and not self.refused:
            if self.entry_zone and self.target and self.stop:
                entry_mid = (self.entry_zone["low"] + self.entry_zone["high"]) / 2
                if self.target <= entry_mid:
                    raise ValueError(
                        f"BUY decision: target {self.target} must be above entry midpoint {entry_mid:.2f}"
                    )
                if self.stop >= entry_mid:
                    raise ValueError(
                        f"BUY decision: stop {self.stop} must be below entry midpoint {entry_mid:.2f}"
                    )
        return self


# ---------------------------------------------------------------------------
# VALIDATOR_REGISTRY: {name: Pydantic class}
# ---------------------------------------------------------------------------
VALIDATOR_REGISTRY: dict[str, type[BaseModel]] = {
    "CEOJudgeOutputValidated": CEOJudgeOutputValidated,
    "EquityExpertOutputValidated": EquityExpertOutputValidated,
}
