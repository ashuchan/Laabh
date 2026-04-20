"""Pydantic schemas for signals."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class SignalResponse(BaseModel):
    id: uuid.UUID
    instrument_id: uuid.UUID
    source_id: uuid.UUID
    analyst_id: uuid.UUID | None
    analyst_name_raw: str | None
    action: str
    timeframe: str
    entry_price: float | None
    target_price: float | None
    stop_loss: float | None
    current_price_at_signal: float | None
    confidence: float | None
    reasoning: str | None
    convergence_score: int
    status: str
    outcome_pnl_pct: float | None
    signal_date: datetime
    expiry_date: datetime | None

    class Config:
        from_attributes = True
