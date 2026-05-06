"""Unified strategy budget — common pool split across equity + F&O brains.

Phase 1 of the multi-brain layout. The *one* paper-capital pool lives in the
``Portfolio.current_cash`` of the equity-strategy portfolio (lumpsum mode);
P&L is realised back into that cash balance so the pool carries forward day
to day automatically.

Each trading day, the LLM equity strategist (09:10 IST) decides how to slice
the pool across four buckets:

    - ``equity``           → the LLM equity strategist itself
    - ``fno_directional``  → long_call, long_put
    - ``fno_spread``       → bull_call_spread, bear_put_spread, iron_condor
    - ``fno_volatility``   → straddle

The allocator persists today's split on the morning ``StrategyDecision``
row's ``actions_json`` under the ``allocations`` key. Downstream consumers
(F&O entry executor, EOD reports) read it back via ``today_allocations()``.
When no allocation has been written for today (system bootstrap, equity
strategy disabled, LLM allocation missing), ``today_allocations()`` falls
back to the defaults configured in ``Settings``.

Bucket capital is **soft**: positions size against the per-bucket ceiling
but realised P&L from any bucket flows back into the shared cash pool.
Carry-forward is automatic; no separate cash counter per bucket.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select

from src.config import get_settings
from src.db import session_scope
from src.models.strategy_decision import StrategyDecision

# F&O strategy_type → bucket key. Keep in sync with src/fno/strategies/base.py.
_STRATEGY_BUCKET = {
    "long_call": "fno_directional",
    "long_put": "fno_directional",
    "bull_call_spread": "fno_spread",
    "bear_put_spread": "fno_spread",
    "iron_condor": "fno_spread",
    "straddle": "fno_volatility",
}

BUCKETS = ("equity", "fno_directional", "fno_spread", "fno_volatility")


@dataclass(frozen=True)
class BudgetPlan:
    """Today's per-bucket capital allocation."""

    total_budget: float
    allocations: dict[str, float]      # bucket_key → fraction (0..1)
    rupee_caps: dict[str, float]       # bucket_key → ₹ ceiling
    source: str                        # "llm" | "default" | "override"
    decided_at: datetime | None
    reasoning: str | None

    def cap_for_strategy(self, fno_strategy_type: str) -> float:
        """Return the rupee ceiling for a given F&O strategy_type string."""
        bucket = bucket_for_fno_strategy(fno_strategy_type)
        return self.rupee_caps.get(bucket, 0.0)


def bucket_for_fno_strategy(strategy_type: str) -> str:
    """Map an F&O strategy_type ('long_call' etc.) to a budget bucket key.

    Unknown strategies fall to ``fno_directional`` so a brand-new strategy
    type doesn't silently get zero capital (loud is better than silent).
    """
    return _STRATEGY_BUCKET.get((strategy_type or "").lower(), "fno_directional")


def _validate_and_normalise(
    raw: dict[str, Any] | None, total: float
) -> tuple[dict[str, float], dict[str, float]]:
    """Coerce a possibly-weird LLM allocation dict into (fractions, rupees).

    Missing buckets contribute 0; values are clamped ≥0; the result is
    re-normalised so fractions sum to exactly 1.0. If the raw input is
    unusable (None, empty, all zeros), returns the configured defaults.
    """
    settings = get_settings()
    defaults = {
        "equity": settings.strategy_default_alloc_equity,
        "fno_directional": settings.strategy_default_alloc_fno_directional,
        "fno_spread": settings.strategy_default_alloc_fno_spread,
        "fno_volatility": settings.strategy_default_alloc_fno_volatility,
    }
    if not isinstance(raw, dict) or not raw:
        fractions = defaults
    else:
        fractions = {b: max(0.0, float(raw.get(b, 0) or 0)) for b in BUCKETS}
        s = sum(fractions.values())
        if s <= 0:
            fractions = defaults
        else:
            fractions = {b: v / s for b, v in fractions.items()}

    # Final renormalisation guards floating-point drift to keep ∑=1.0.
    s = sum(fractions.values()) or 1.0
    fractions = {b: v / s for b, v in fractions.items()}
    rupee_caps = {b: round(total * v, 2) for b, v in fractions.items()}
    return fractions, rupee_caps


def default_plan() -> BudgetPlan:
    """Fallback allocation derived purely from config — no LLM input."""
    settings = get_settings()
    fractions, rupee_caps = _validate_and_normalise(None, settings.strategy_total_budget)
    return BudgetPlan(
        total_budget=settings.strategy_total_budget,
        allocations=fractions,
        rupee_caps=rupee_caps,
        source="default",
        decided_at=None,
        reasoning=None,
    )


async def today_allocations(as_of: datetime | None = None) -> BudgetPlan:
    """Return today's BudgetPlan — LLM-decided if present, else default.

    Looks for the morning_allocation StrategyDecision row whose ``as_of`` is
    today (UTC) and reads ``actions_json['allocations']``. The LLM emits the
    allocations as fractions per bucket; if any are missing they fall to
    config defaults. Total budget is read from settings (lumpsum, carries
    forward via portfolio cash so the *cap* doesn't re-deploy realised P&L
    against itself — the pool grows organically).
    """
    settings = get_settings()
    eff = as_of or datetime.now(tz=timezone.utc)
    today = eff.date()
    day_start = datetime.combine(today, time.min, tzinfo=timezone.utc)
    day_end = datetime.combine(today, time.max, tzinfo=timezone.utc)
    async with session_scope() as session:
        row = (
            await session.execute(
                select(StrategyDecision)
                .where(
                    StrategyDecision.decision_type == "morning_allocation",
                    StrategyDecision.as_of >= day_start,
                    StrategyDecision.as_of <= day_end,
                    StrategyDecision.dryrun_run_id.is_(None),
                )
                .order_by(StrategyDecision.as_of.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    if row is None:
        return default_plan()

    actions_json = row.actions_json or {}
    raw_alloc = actions_json.get("allocations") if isinstance(actions_json, dict) else None
    fractions, rupee_caps = _validate_and_normalise(
        raw_alloc, settings.strategy_total_budget
    )
    source = "llm" if isinstance(raw_alloc, dict) and raw_alloc else "default"
    return BudgetPlan(
        total_budget=settings.strategy_total_budget,
        allocations=fractions,
        rupee_caps=rupee_caps,
        source=source,
        decided_at=row.as_of,
        reasoning=row.llm_reasoning,
    )


async def fno_premium_deployed_today(
    bucket: str, as_of: datetime | None = None
) -> float:
    """Sum of today's `entry_premium_net` for FNO signals in this bucket.

    Used by the entry executor to compute remaining bucket headroom before
    sizing a new position. Only signals filled today are considered (open
    positions carried over from prior days are accounted for via realised
    P&L, not by re-charging their premium against today's bucket).
    """
    from sqlalchemy import func

    from src.models.fno_signal import FNOSignal

    eff = as_of or datetime.now(tz=timezone.utc)
    target_strategies = [s for s, b in _STRATEGY_BUCKET.items() if b == bucket]
    if not target_strategies:
        return 0.0
    async with session_scope() as session:
        res = await session.execute(
            select(func.coalesce(func.sum(FNOSignal.entry_premium_net), 0))
            .where(
                func.date(FNOSignal.proposed_at) == eff.date(),
                FNOSignal.strategy_type.in_(target_strategies),
                FNOSignal.dryrun_run_id.is_(None),
            )
        )
        return float(res.scalar() or 0)


async def remaining_capacity(
    bucket: str, as_of: datetime | None = None
) -> float:
    """Headroom (₹) left in `bucket` today after subtracting filled premium."""
    plan = await today_allocations(as_of)
    cap = plan.rupee_caps.get(bucket, 0.0)
    used = await fno_premium_deployed_today(bucket, as_of)
    return max(0.0, cap - used)


def stamp_allocations_into_actions_json(
    actions_json: dict[str, Any] | None,
    raw_alloc: dict[str, Any] | None,
) -> dict[str, Any]:
    """Splice a normalised allocation block into the LLM's actions_json.

    Returns the (possibly new) dict ready to persist on the StrategyDecision
    row. The morning equity strategist calls this just before insert so the
    F&O entry executor at 09:15 can read it back. Defaults are filled in so
    a partial LLM output still produces a complete plan.
    """
    settings = get_settings()
    fractions, rupee_caps = _validate_and_normalise(
        raw_alloc, settings.strategy_total_budget
    )
    block = {
        "total_budget": settings.strategy_total_budget,
        "fractions": fractions,
        "rupee_caps": rupee_caps,
        "source": "llm" if isinstance(raw_alloc, dict) and raw_alloc else "default",
    }
    out = dict(actions_json or {})
    out["allocations"] = block
    return out


__all__ = [
    "BUCKETS",
    "BudgetPlan",
    "bucket_for_fno_strategy",
    "default_plan",
    "fno_premium_deployed_today",
    "remaining_capacity",
    "stamp_allocations_into_actions_json",
    "today_allocations",
]
