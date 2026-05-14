"""LLM-feature monitoring + rollback triggers — Phase 4.

Plan reference: docs/llm_feature_generator/implementation_plan.md §4.

Provides two things:

  1. Computation helpers the dashboard and the rollback-trigger job can call
     to summarise the v10 pipeline's health (feature drift, calibration ECE,
     three-way Sharpe comparison, bandit-coefficient stability).
  2. :func:`check_rollback_triggers` which evaluates the table in plan §4.2
     and writes a structured advisory to the log + a notification row when
     any hard trigger fires. The function does NOT auto-flip
     ``LAABH_LLM_MODE`` — that's a deliberate human-in-the-loop decision.

The module is read-only against ``llm_decision_log`` /
``llm_calibration_models`` / ``fno_signals`` / ``quant_universe_baseline``;
nothing here mutates the live trading state.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

import numpy as np
from loguru import logger
from sqlalchemy import text

from src.db import session_scope


# Plan §4.2 thresholds.
_SHARPE_HARD_ROLLBACK_RATIO = 0.7
_DRAWDOWN_HARD_ROLLBACK_RATIO = 1.5
_ECE_HARD_ROLLBACK = 0.15
_COST_PER_TRADE_ROLLBACK_RATIO = 3.0
_BANDIT_COEF_DEAD_BAND_SIGMA = 2.0
_BANDIT_COEF_DEAD_BAND_DAYS = 20

# Rolling windows
_ROLLING_SHARPE_DAYS = 30
_ROLLING_DRAWDOWN_DAYS = 30
_ROLLING_COST_DAYS = 7

# Cost model — Claude Sonnet snapshot prices in INR per token, used as a
# rough comparator for the cost-rollback trigger (plan §4.2 / O5). The
# absolute number is less important than the v9-vs-v10 ratio, so a fixed
# multiplier is acceptable for the rollback advisory.
_INR_PER_INPUT_TOKEN = 0.0003
_INR_PER_OUTPUT_TOKEN = 0.0015


@dataclass(frozen=True)
class RollbackAdvisory:
    """Structured result returned by :func:`check_rollback_triggers`."""

    fired: bool
    hard_triggers: list[str] = field(default_factory=list)
    soft_triggers: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Top-level helpers
# ---------------------------------------------------------------------------


async def feature_drift_summary(
    *,
    feature_names: Iterable[str] = (
        "directional_conviction",
        "thesis_durability",
        "catalyst_specificity",
        "risk_flag",
    ),
    days: int = 28,
) -> dict[str, dict]:
    """Rolling weekly-mean for each LLM feature over the last ``days`` days.

    Returns ``{feature_name: {week_index: mean_value}}``. The dashboard can
    plot this to spot prompt regression (>0.15 absolute shift month-on-month
    per plan §4.1).
    """
    sql = text("""
        SELECT
            DATE_TRUNC('week', run_date)::DATE AS week_start,
            AVG(directional_conviction)        AS dc,
            AVG(thesis_durability)             AS td,
            AVG(catalyst_specificity)          AS cs,
            AVG(risk_flag)                     AS rf
        FROM llm_decision_log
        WHERE prompt_version = 'v10_continuous'
          AND run_date >= :since
        GROUP BY week_start
        ORDER BY week_start
    """)
    since = (datetime.now(tz=timezone.utc) - timedelta(days=days)).date()
    async with session_scope() as session:
        rows = (await session.execute(sql, {"since": since})).mappings().all()

    by_feature = {f: {} for f in feature_names}
    for r in rows:
        wk = r["week_start"].isoformat()
        by_feature.get("directional_conviction", {})[wk] = _safe(r["dc"])
        by_feature.get("thesis_durability", {})[wk] = _safe(r["td"])
        by_feature.get("catalyst_specificity", {})[wk] = _safe(r["cs"])
        by_feature.get("risk_flag", {})[wk] = _safe(r["rf"])
    return by_feature


async def active_calibration_ece() -> dict[str, float | None]:
    """Latest active model's ECE per (feature_name, instrument_tier)."""
    sql = text("""
        SELECT feature_name, instrument_tier, cv_ece
        FROM llm_calibration_models
        WHERE is_active = TRUE
    """)
    async with session_scope() as session:
        rows = (await session.execute(sql)).mappings().all()
    return {
        f"{r['feature_name']}/{r['instrument_tier']}": (
            float(r["cv_ece"]) if r["cv_ece"] is not None else None
        )
        for r in rows
    }


async def three_way_sharpe_compare(*, days: int = _ROLLING_SHARPE_DAYS) -> dict[str, float | None]:
    """Rolling Sharpe per pipeline.

    Returns dict with keys ``'v9_gate'``, ``'v10_feature'``, ``'deterministic'``
    each carrying the rolling annualised Sharpe over ``days`` days or None
    if there isn't enough data. P&L source for each pipeline:

      * v9_gate / v10_feature: ``fno_signals.final_pnl`` for closed signals
        whose upstream candidate came from the corresponding LLM prompt
        version.
      * deterministic: hypothetical equal-weight P&L on
        ``quant_universe_baseline`` top-K using next-day returns (cheap;
        the bandit's actual deployment is in v10_feature).
    """
    pnl_sql = text("""
        SELECT s.proposed_at::DATE AS day, l.prompt_version AS pv, s.final_pnl
        FROM fno_signals s
        JOIN fno_candidates c ON c.id = s.candidate_id
        LEFT JOIN llm_decision_log l
          ON l.run_date = c.run_date
         AND l.instrument_id = c.instrument_id
         AND l.phase = 'fno_thesis'
        WHERE s.status = 'closed'
          AND s.closed_at >= :since
    """)
    since = datetime.now(tz=timezone.utc) - timedelta(days=days)
    async with session_scope() as session:
        rows = (await session.execute(pnl_sql, {"since": since})).all()

    pv_to_pnls: dict[str, list[float]] = {"v9": [], "v10_continuous": []}
    for r in rows:
        bucket = pv_to_pnls.setdefault(r.pv or "v9", [])
        if r.final_pnl is not None:
            bucket.append(float(r.final_pnl))

    out: dict[str, float | None] = {}
    out["v9_gate"] = _sharpe(pv_to_pnls.get("v9", []))
    out["v10_feature"] = _sharpe(pv_to_pnls.get("v10_continuous", []))
    out["deterministic"] = None  # TODO(phase5): wire baseline next-day P&L
    return out


async def max_drawdown_compare(*, days: int = _ROLLING_DRAWDOWN_DAYS) -> dict[str, float | None]:
    """Per-pipeline max drawdown over the rolling window.

    Drawdown is computed on the equity curve = running cumsum of per-trade
    P&L grouped by close-date. Returns the worst peak-to-trough drop as a
    positive fraction of peak equity.
    """
    sql = text("""
        SELECT s.closed_at::DATE AS day,
               COALESCE(l.prompt_version, 'v9') AS pv,
               COALESCE(s.final_pnl, 0)::FLOAT AS pnl
        FROM fno_signals s
        JOIN fno_candidates c ON c.id = s.candidate_id
        LEFT JOIN llm_decision_log l
          ON l.run_date = c.run_date
         AND l.instrument_id = c.instrument_id
         AND l.phase = 'fno_thesis'
         AND l.prompt_version != 'v9'   -- prefer v10 row when present
        WHERE s.status = 'closed'
          AND s.closed_at >= :since
        ORDER BY day, pv
    """)
    since = datetime.now(tz=timezone.utc) - timedelta(days=days)
    async with session_scope() as session:
        rows = (await session.execute(sql, {"since": since})).all()

    pv_to_pnls: dict[str, list[float]] = {}
    for r in rows:
        pv_to_pnls.setdefault(r.pv or "v9", []).append(float(r.pnl))

    return {
        "v9_gate": _max_drawdown(pv_to_pnls.get("v9", [])),
        "v10_feature": _max_drawdown(pv_to_pnls.get("v10_continuous", [])),
    }


async def cost_per_trade_compare(*, days: int = _ROLLING_COST_DAYS) -> dict[str, float | None]:
    """Per-caller average INR cost of a Phase-3 LLM call over ``days`` days.

    Reads tokens from ``llm_audit_log`` grouped by ``caller`` (v9 writes
    ``fno.thesis_synthesizer``; v10 shadow writes ``fno.thesis_synthesizer.v10``).
    Returns both unit costs plus the v10/v9 ratio so the rollback trigger
    has real measurements rather than the previous hardcoded constant
    (review fix P0 #1). Either side returns None when no calls were
    logged in the window — the trigger then declines to fire.
    """
    sql = text("""
        SELECT
            caller,
            COALESCE(SUM(tokens_in), 0)  AS tok_in,
            COALESCE(SUM(tokens_out), 0) AS tok_out,
            COUNT(*)                     AS n_calls
        FROM llm_audit_log
        WHERE caller IN ('fno.thesis_synthesizer', 'fno.thesis_synthesizer.v10')
          AND created_at >= :since
        GROUP BY caller
    """)
    since = datetime.now(tz=timezone.utc) - timedelta(days=days)
    async with session_scope() as session:
        rows = (await session.execute(sql, {"since": since})).all()

    by_caller: dict[str, float | None] = {
        "fno.thesis_synthesizer": None,
        "fno.thesis_synthesizer.v10": None,
    }
    for r in rows:
        if r.n_calls <= 0:
            continue
        avg_in = float(r.tok_in) / r.n_calls
        avg_out = float(r.tok_out) / r.n_calls
        unit = avg_in * _INR_PER_INPUT_TOKEN + avg_out * _INR_PER_OUTPUT_TOKEN
        by_caller[r.caller] = float(unit)

    v9_unit = by_caller["fno.thesis_synthesizer"]
    v10_unit = by_caller["fno.thesis_synthesizer.v10"]
    ratio: float | None
    if v9_unit is not None and v10_unit is not None and v9_unit > 0:
        ratio = v10_unit / v9_unit
    else:
        ratio = None
    return {
        "v9_call_inr": v9_unit,
        "v10_call_inr": v10_unit,
        "ratio": ratio,
    }


async def check_rollback_triggers() -> RollbackAdvisory:
    """Evaluate plan §4.2 trigger table. Logs + returns an advisory.

    Hard triggers:
      - 30-day v10 Sharpe < 0.7× deterministic Sharpe
      - v10 max drawdown > 1.5× v9 shadow drawdown
      - Calibration ECE > 0.15 across all active models

    Soft triggers:
      - LLM cost per trade > 3× v9 baseline
      - Bandit LLM-dim coefficients within ±2σ of zero for 20 days
        (TODO: requires bandit_arm_state.theta history scan — flagged
        for a follow-up rather than approximated here.)
    """
    advisory_details: dict = {}
    hard_triggers: list[str] = []
    soft_triggers: list[str] = []

    sharpes = await three_way_sharpe_compare()
    advisory_details["sharpes"] = sharpes
    v10 = sharpes.get("v10_feature")
    det = sharpes.get("deterministic")
    if v10 is not None and det is not None and v10 < _SHARPE_HARD_ROLLBACK_RATIO * det:
        hard_triggers.append(
            f"v10 30d Sharpe {v10:.2f} < {_SHARPE_HARD_ROLLBACK_RATIO}× deterministic ({det:.2f})"
        )

    # Drawdown trigger (review fix P2 #7).
    drawdowns = await max_drawdown_compare()
    advisory_details["drawdowns"] = drawdowns
    dd_v9 = drawdowns.get("v9_gate")
    dd_v10 = drawdowns.get("v10_feature")
    if dd_v9 is not None and dd_v10 is not None and dd_v9 > 0:
        if dd_v10 > _DRAWDOWN_HARD_ROLLBACK_RATIO * dd_v9:
            hard_triggers.append(
                f"v10 max DD {dd_v10:.2%} > {_DRAWDOWN_HARD_ROLLBACK_RATIO}× v9 DD ({dd_v9:.2%})"
            )

    eces = await active_calibration_ece()
    advisory_details["ece"] = eces
    if eces:
        present = [v for v in eces.values() if v is not None]
        if present and all(v > _ECE_HARD_ROLLBACK for v in present):
            hard_triggers.append(
                f"all active calibration ECE > {_ECE_HARD_ROLLBACK} "
                f"(values: {present})"
            )

    # Cost trigger — soft (review = "review, not auto-rollback").
    costs = await cost_per_trade_compare()
    advisory_details["costs"] = costs
    ratio = costs.get("ratio")
    if ratio is not None and ratio > _COST_PER_TRADE_ROLLBACK_RATIO:
        soft_triggers.append(
            f"v10 cost per trade is {ratio:.1f}× v9 (> {_COST_PER_TRADE_ROLLBACK_RATIO}×) — review"
        )

    fired = bool(hard_triggers)
    if fired:
        logger.warning(f"llm_monitoring: rollback advisory FIRED — {hard_triggers}")
    elif soft_triggers:
        logger.info(f"llm_monitoring: soft advisory — {soft_triggers}")
    return RollbackAdvisory(
        fired=fired,
        hard_triggers=hard_triggers,
        soft_triggers=soft_triggers,
        details=advisory_details,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


def _sharpe(pnls: list[float], *, periods_per_year: int = 252) -> float | None:
    """Annualised Sharpe from a per-trade-day P&L list.

    Treats consecutive trades as independent — fine for the dashboard's
    summary view; backtests use a more rigorous calculation.
    """
    if len(pnls) < 5:
        return None
    arr = np.array(pnls, dtype=float)
    std = float(arr.std())
    if std == 0.0:
        return None
    return float(arr.mean() / std * math.sqrt(periods_per_year))


def _max_drawdown(pnls: list[float]) -> float | None:
    """Max peak-to-trough drop on the cumulative-P&L equity curve.

    Returned as a positive fraction of the running peak (e.g. 0.20 = 20%
    drawdown). Returns None on insufficient data or a strictly positive
    equity curve (no drawdown).
    """
    if len(pnls) < 5:
        return None
    arr = np.cumsum(np.array(pnls, dtype=float))
    # Anchor the curve so an all-positive run has a 0 peak baseline.
    arr = np.concatenate([[0.0], arr])
    peak = np.maximum.accumulate(arr)
    # Avoid divide-by-zero — drawdown is undefined when peak hasn't moved
    # above 0 yet. Use peak in absolute terms over 1 so we keep returns
    # meaningful even for small books.
    denom = np.maximum(peak, 1.0)
    dd = (peak - arr) / denom
    worst = float(np.max(dd))
    return worst if worst > 0 else None
