"""Analyst leaderboard routes."""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select

from src.db import session_scope
from src.models.analyst import Analyst

router = APIRouter(prefix="/analysts", tags=["analysts"])


class AnalystResponse(BaseModel):
    id: uuid.UUID
    name: str
    organization: str | None
    total_signals: int
    hit_rate: float
    avg_return_pct: float
    credibility_score: float
    best_sector: str | None
    is_active: bool
    updated_at: datetime

    class Config:
        from_attributes = True


@router.get("/leaderboard", response_model=list[AnalystResponse])
async def analyst_leaderboard(limit: int = Query(20, le=100)):
    """Return analysts sorted by credibility score."""
    async with session_scope() as session:
        result = await session.execute(
            select(Analyst)
            .where(Analyst.is_active == True)
            .order_by(Analyst.credibility_score.desc())
            .limit(limit)
        )
        analysts = result.scalars().all()
    return [AnalystResponse.model_validate(a) for a in analysts]


@router.get("/{analyst_id}", response_model=AnalystResponse)
async def get_analyst(analyst_id: uuid.UUID):
    from fastapi import HTTPException
    async with session_scope() as session:
        analyst = await session.get(Analyst, analyst_id)
    if analyst is None:
        raise HTTPException(status_code=404, detail="Analyst not found")
    return AnalystResponse.model_validate(analyst)
