"""Portfolio routes."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from src.api.schemas.portfolio import HoldingResponse, PortfolioResponse, SnapshotResponse
from src.db import session_scope
from src.models.portfolio import Holding, Portfolio, PortfolioSnapshot

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


async def _get_portfolio() -> Portfolio:
    async with session_scope() as session:
        result = await session.execute(
            select(Portfolio).where(Portfolio.is_active == True).limit(1)
        )
        p = result.scalar_one_or_none()
    if p is None:
        raise HTTPException(status_code=404, detail="No active portfolio")
    return p


@router.get("/", response_model=PortfolioResponse)
async def get_portfolio():
    """Return current portfolio summary."""
    return PortfolioResponse.model_validate(await _get_portfolio())


@router.get("/holdings", response_model=list[HoldingResponse])
async def get_holdings():
    """Return all current holdings."""
    portfolio = await _get_portfolio()
    async with session_scope() as session:
        result = await session.execute(
            select(Holding).where(Holding.portfolio_id == portfolio.id)
        )
        holdings = result.scalars().all()
    return [HoldingResponse.model_validate(h) for h in holdings]


@router.get("/history", response_model=list[SnapshotResponse])
async def get_history(days: int = Query(30, le=365)):
    """Return daily portfolio snapshots for charting."""
    portfolio = await _get_portfolio()
    async with session_scope() as session:
        result = await session.execute(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.portfolio_id == portfolio.id)
            .order_by(PortfolioSnapshot.date.desc())
            .limit(days)
        )
        snaps = result.scalars().all()
    return [SnapshotResponse.model_validate(s) for s in snaps]
