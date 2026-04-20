"""Pydantic schemas for portfolio and holdings."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel


class HoldingResponse(BaseModel):
    id: uuid.UUID
    instrument_id: uuid.UUID
    quantity: int
    avg_buy_price: float
    invested_value: float
    current_price: float | None
    current_value: float | None
    pnl: float | None
    pnl_pct: float | None
    day_change_pct: float | None
    weight_pct: float | None

    class Config:
        from_attributes = True


class PortfolioResponse(BaseModel):
    id: uuid.UUID
    name: str
    initial_capital: float
    current_cash: float
    invested_value: float
    current_value: float
    total_pnl: float
    total_pnl_pct: float
    day_pnl: float
    benchmark_symbol: str
    total_trades: int
    win_rate: float
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class SnapshotResponse(BaseModel):
    date: date
    total_value: float | None
    cash: float | None
    invested_value: float | None
    day_pnl: float | None
    day_pnl_pct: float | None
    cumulative_pnl_pct: float | None
    benchmark_value: float | None
    benchmark_pnl_pct: float | None
    num_holdings: int | None

    class Config:
        from_attributes = True
