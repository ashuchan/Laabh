"""Pydantic schemas for trade request/response."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class PlaceOrderRequest(BaseModel):
    instrument_id: uuid.UUID
    trade_type: Literal["BUY", "SELL"]
    order_type: Literal["MARKET", "LIMIT", "STOP_LOSS", "STOP_LOSS_MARKET"] = "MARKET"
    quantity: int = Field(gt=0)
    limit_price: Decimal | None = None
    trigger_price: Decimal | None = None
    signal_id: uuid.UUID | None = None
    reason: str | None = None


class TradeResponse(BaseModel):
    id: uuid.UUID
    portfolio_id: uuid.UUID
    instrument_id: uuid.UUID
    signal_id: uuid.UUID | None
    trade_type: str
    order_type: str
    quantity: int
    price: float
    brokerage: float
    stt: float
    total_cost: float | None
    status: str
    executed_at: datetime | None
    created_at: datetime

    class Config:
        from_attributes = True


class PendingOrderResponse(BaseModel):
    id: uuid.UUID
    portfolio_id: uuid.UUID
    instrument_id: uuid.UUID
    trade_type: str
    order_type: str
    quantity: int
    limit_price: float | None
    trigger_price: float | None
    status: str
    valid_till: datetime | None
    created_at: datetime

    class Config:
        from_attributes = True
