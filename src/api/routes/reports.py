"""Reports & system-health routes — surface daily-rollup and pipeline data to the mobile app."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from src.api.schemas.fno import SourceHealthResponse
from src.db import session_scope
from src.models.fno_chain_issue import ChainCollectionIssue
from src.models.fno_source_health import SourceHealth
from src.models.instrument import Instrument
from src.models.strategy_decision import StrategyDecision
from src.runday.checks.chain import get_tier_breakdown
from src.runday.config import get_runday_settings
from src.runday.scripts.daily_report import build_report

router = APIRouter(prefix="/reports", tags=["reports"])


# ---------------------------------------------------------------------------
# Daily report
# ---------------------------------------------------------------------------


@router.get("/daily")
async def get_daily_report(
    report_date: date | None = Query(None, alias="date", description="YYYY-MM-DD; defaults to today"),
):
    """Return the structured EOD rollup — pipeline, chain health, LLM, trading, surprises."""
    target = report_date or date.today()
    return await build_report(target)


# ---------------------------------------------------------------------------
# Tier coverage / system health
# ---------------------------------------------------------------------------


class TierRow(BaseModel):
    symbol: str
    tier: int
    last_attempt: str | None
    last_status: str | None
    success_rate_1h: float | None
    source_breakdown: dict[str, int]


@router.get("/tier-coverage", response_model=list[TierRow])
async def get_tier_coverage(
    lookback_minutes: int = Query(60, ge=5, le=1440),
    tier: int | None = Query(None, ge=1, le=2),
    only_degraded: bool = False,
    limit: int = Query(100, le=500),
):
    """Per-instrument chain coverage diagnostic."""
    settings = get_runday_settings()
    rows = await get_tier_breakdown(
        settings,
        lookback_minutes=lookback_minutes,
        tier_filter=tier,
        only_degraded=only_degraded,
        limit=limit,
    )
    return [TierRow.model_validate(r) for r in rows]


@router.get("/source-health", response_model=list[SourceHealthResponse])
async def get_source_health():
    """Current health for all chain data sources."""
    async with session_scope() as session:
        result = await session.execute(select(SourceHealth))
        rows = result.scalars().all()
    return [SourceHealthResponse.model_validate(r) for r in rows]


# ---------------------------------------------------------------------------
# Strategy decisions
# ---------------------------------------------------------------------------


class StrategyDecisionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    portfolio_id: uuid.UUID
    decision_type: str
    as_of: datetime
    risk_profile: str | None
    budget_available: float | None
    llm_model: str | None
    llm_reasoning: str | None
    actions_executed: int
    actions_skipped: int
    created_at: datetime


@router.get("/strategy-decisions", response_model=list[StrategyDecisionResponse])
async def list_strategy_decisions(
    decision_date: date | None = Query(None, alias="date"),
    decision_type: str | None = Query(None, pattern="^(morning_allocation|intraday_action|eod_squareoff)$"),
    limit: int = Query(50, le=200),
):
    """List strategy decisions, optionally filtered by date or type."""
    from datetime import time, timedelta

    async with session_scope() as session:
        q = (
            select(StrategyDecision)
            .where(StrategyDecision.dryrun_run_id.is_(None))
            .order_by(StrategyDecision.as_of.desc())
        )
        if decision_date is not None:
            day_start = datetime.combine(decision_date, time.min, tzinfo=timezone.utc)
            day_end = day_start + timedelta(days=1)
            q = q.where(
                StrategyDecision.as_of >= day_start,
                StrategyDecision.as_of < day_end,
            )
        if decision_type:
            q = q.where(StrategyDecision.decision_type == decision_type)
        q = q.limit(limit)
        rows = (await session.execute(q)).scalars().all()
    return [StrategyDecisionResponse.model_validate(r) for r in rows]


# ---------------------------------------------------------------------------
# Signal performance
# ---------------------------------------------------------------------------


class SignalPerformanceRow(BaseModel):
    id: uuid.UUID
    instrument_id: uuid.UUID
    symbol: str | None
    action: str
    status: str
    entry_price: float | None
    target_price: float | None
    outcome_pnl_pct: float | None
    convergence_score: int
    confidence: float | None
    analyst_id: uuid.UUID | None
    analyst_name_raw: str | None
    signal_date: datetime


class SignalPerformanceSummary(BaseModel):
    total: int
    resolved: int
    hits: int
    misses: int
    hit_rate: float
    avg_pnl_pct: float | None
    rows: list[SignalPerformanceRow]


@router.get("/signal-performance", response_model=SignalPerformanceSummary)
async def get_signal_performance(
    days: int = Query(30, ge=1, le=365),
    analyst_id: uuid.UUID | None = None,
    limit: int = Query(100, le=500),
):
    """Aggregate hit/miss + per-signal outcome over a window."""
    from datetime import timedelta

    from src.models.signal import Signal

    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with session_scope() as session:
        q = (
            select(Signal, Instrument.symbol)
            .join(Instrument, Signal.instrument_id == Instrument.id, isouter=True)
            .where(Signal.signal_date >= since)
            .order_by(Signal.signal_date.desc())
        )
        if analyst_id:
            q = q.where(Signal.analyst_id == analyst_id)
        q = q.limit(limit)
        rows = (await session.execute(q)).all()

    out_rows: list[SignalPerformanceRow] = []
    resolved = 0
    hits = 0
    pnl_sum = 0.0
    pnl_count = 0
    for sig, symbol in rows:
        out_rows.append(
            SignalPerformanceRow(
                id=sig.id,
                instrument_id=sig.instrument_id,
                symbol=symbol,
                action=sig.action,
                status=sig.status,
                entry_price=float(sig.entry_price) if sig.entry_price is not None else None,
                target_price=float(sig.target_price) if sig.target_price is not None else None,
                outcome_pnl_pct=float(sig.outcome_pnl_pct) if sig.outcome_pnl_pct is not None else None,
                convergence_score=sig.convergence_score or 0,
                confidence=float(sig.confidence) if sig.confidence is not None else None,
                analyst_id=sig.analyst_id,
                analyst_name_raw=sig.analyst_name_raw,
                signal_date=sig.signal_date,
            )
        )
        if sig.outcome_pnl_pct is not None:
            resolved += 1
            pct = float(sig.outcome_pnl_pct)
            pnl_sum += pct
            pnl_count += 1
            if pct > 0:
                hits += 1

    misses = resolved - hits
    return SignalPerformanceSummary(
        total=len(out_rows),
        resolved=resolved,
        hits=hits,
        misses=misses,
        hit_rate=(hits / resolved) if resolved else 0.0,
        avg_pnl_pct=(pnl_sum / pnl_count) if pnl_count else None,
        rows=out_rows,
    )


# ---------------------------------------------------------------------------
# Chain issues (read + resolve already exists under /fno; expose a thin wrapper here too)
# ---------------------------------------------------------------------------


class ChainIssueRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: str
    instrument_id: uuid.UUID | None
    issue_type: str
    error_message: str
    detected_at: datetime | None
    github_issue_url: str | None
    resolved_at: datetime | None


@router.get("/chain-issues", response_model=list[ChainIssueRow])
async def list_chain_issues(
    status: str = Query("open", pattern="^(open|resolved|all)$"),
    limit: int = Query(50, le=200),
):
    """Open or resolved chain ingestion issues, newest first."""
    async with session_scope() as session:
        q = select(ChainCollectionIssue).order_by(ChainCollectionIssue.detected_at.desc())
        if status == "open":
            q = q.where(ChainCollectionIssue.resolved_at.is_(None))
        elif status == "resolved":
            q = q.where(ChainCollectionIssue.resolved_at.isnot(None))
        q = q.limit(limit)
        rows = (await session.execute(q)).scalars().all()
    return [ChainIssueRow.model_validate(r) for r in rows]
