"""F&O intelligence module API routes."""
from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from src.api.schemas.fno import (
    FNOBanListResponse,
    FNOCandidateResponse,
    IVHistoryResponse,
    PipelineTriggerResponse,
    VIXTickResponse,
)
from src.db import session_scope
from src.models.fno_ban import FNOBanList
from src.models.fno_candidate import FNOCandidate
from src.models.fno_iv import IVHistory
from src.models.fno_vix import VIXTick
from src.models.instrument import Instrument

router = APIRouter(prefix="/fno", tags=["fno"])


@router.get("/candidates", response_model=list[FNOCandidateResponse])
async def list_candidates(
    run_date: date | None = None,
    phase: int | None = Query(None, ge=1, le=3),
    passed_only: bool = False,
    limit: int = Query(50, le=200),
    offset: int = 0,
):
    """List F&O pipeline candidates with optional filters."""
    async with session_scope() as session:
        q = (
            select(FNOCandidate, Instrument.symbol)
            .join(Instrument, FNOCandidate.instrument_id == Instrument.id)
            .order_by(FNOCandidate.run_date.desc(), FNOCandidate.composite_score.desc())
        )
        if run_date:
            q = q.where(FNOCandidate.run_date == run_date)
        if phase is not None:
            q = q.where(FNOCandidate.phase == phase)
        if passed_only:
            q = q.where(FNOCandidate.passed_liquidity == True)  # noqa: E712
        q = q.limit(limit).offset(offset)
        rows = (await session.execute(q)).all()

    results = []
    for cand, symbol in rows:
        resp = FNOCandidateResponse.model_validate(cand)
        resp.symbol = symbol
        results.append(resp)
    return results


@router.get("/candidates/{candidate_id}", response_model=FNOCandidateResponse)
async def get_candidate(candidate_id: uuid.UUID):
    """Get a specific F&O candidate by ID."""
    async with session_scope() as session:
        row = await session.execute(
            select(FNOCandidate, Instrument.symbol)
            .join(Instrument, FNOCandidate.instrument_id == Instrument.id)
            .where(FNOCandidate.id == candidate_id)
        )
        result = row.first()

    if not result:
        raise HTTPException(status_code=404, detail="Candidate not found")
    cand, symbol = result
    resp = FNOCandidateResponse.model_validate(cand)
    resp.symbol = symbol
    return resp


@router.get("/iv-history/{instrument_id}", response_model=list[IVHistoryResponse])
async def get_iv_history(
    instrument_id: uuid.UUID,
    limit: int = Query(52, le=260),
):
    """Get IV history for an instrument (most recent first)."""
    async with session_scope() as session:
        result = await session.execute(
            select(IVHistory)
            .where(IVHistory.instrument_id == instrument_id)
            .order_by(IVHistory.date.desc())
            .limit(limit)
        )
        rows = result.scalars().all()
    return [IVHistoryResponse.model_validate(r) for r in rows]


@router.get("/vix", response_model=list[VIXTickResponse])
async def get_vix_history(limit: int = Query(10, le=100)):
    """Get recent India VIX readings."""
    async with session_scope() as session:
        result = await session.execute(
            select(VIXTick)
            .order_by(VIXTick.timestamp.desc())
            .limit(limit)
        )
        rows = result.scalars().all()
    return [VIXTickResponse.model_validate(r) for r in rows]


@router.get("/ban-list", response_model=list[FNOBanListResponse])
async def get_ban_list(active_only: bool = True):
    """Get current F&O ban list."""
    async with session_scope() as session:
        q = select(FNOBanList).order_by(FNOBanList.ban_date.desc())
        if active_only:
            q = q.where(FNOBanList.is_active == True)  # noqa: E712
        result = await session.execute(q)
        rows = result.scalars().all()
    return [FNOBanListResponse.model_validate(r) for r in rows]


@router.post("/pipeline/trigger", response_model=PipelineTriggerResponse)
async def trigger_pipeline(run_date: date | None = None):
    """Manually trigger the pre-market pipeline (Phase 1-3).

    This is for testing only — in production the pipeline is scheduler-driven.
    """
    from src.fno.orchestrator import run_premarket_pipeline
    result = await run_premarket_pipeline(run_date)
    if result.get("skipped"):
        return PipelineTriggerResponse(
            status="skipped_module_disabled",
            run_date=(run_date or date.today()).isoformat(),
        )
    return PipelineTriggerResponse(
        status="ok",
        run_date=result["run_date"],
        phase1_passed=result["phase1_passed"],
        phase2_passed=result["phase2_passed"],
        phase3_proceed=result["phase3_proceed"],
    )
