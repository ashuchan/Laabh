"""Signal routes."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Query
from sqlalchemy import select

from src.api.schemas.signal import SignalResponse
from src.db import session_scope
from src.models.signal import Signal

router = APIRouter(prefix="/signals", tags=["signals"])


@router.get("/", response_model=list[SignalResponse])
async def list_signals(
    status: str | None = None,
    action: str | None = None,
    instrument_id: uuid.UUID | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
):
    """List signals with optional filters."""
    async with session_scope() as session:
        q = select(Signal).order_by(Signal.signal_date.desc())
        if status:
            q = q.where(Signal.status == status)
        if action:
            q = q.where(Signal.action == action)
        if instrument_id:
            q = q.where(Signal.instrument_id == instrument_id)
        q = q.offset(offset).limit(limit)
        result = await session.execute(q)
        signals = result.scalars().all()
    return [SignalResponse.model_validate(s) for s in signals]


@router.get("/active", response_model=list[SignalResponse])
async def list_active_signals(limit: int = Query(50, le=200)):
    """List only active signals, sorted by convergence score."""
    async with session_scope() as session:
        result = await session.execute(
            select(Signal)
            .where(Signal.status == "active")
            .order_by(Signal.convergence_score.desc(), Signal.signal_date.desc())
            .limit(limit)
        )
        signals = result.scalars().all()
    return [SignalResponse.model_validate(s) for s in signals]


@router.get("/{signal_id}", response_model=SignalResponse)
async def get_signal(signal_id: uuid.UUID):
    """Return a single signal by ID."""
    from fastapi import HTTPException
    async with session_scope() as session:
        signal = await session.get(Signal, signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="Signal not found")
    return SignalResponse.model_validate(signal)
