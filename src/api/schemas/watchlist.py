"""Pydantic schemas for watchlists."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class WatchlistResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    is_default: bool
    created_at: datetime

    class Config:
        from_attributes = True


class WatchlistItemResponse(BaseModel):
    id: uuid.UUID
    watchlist_id: uuid.UUID
    instrument_id: uuid.UUID
    target_buy_price: float | None
    target_sell_price: float | None
    price_alert_above: float | None
    price_alert_below: float | None
    alert_on_signals: bool
    notes: str | None

    class Config:
        from_attributes = True


class AddWatchlistItemRequest(BaseModel):
    instrument_id: uuid.UUID
    target_buy_price: float | None = None
    target_sell_price: float | None = None
    price_alert_above: float | None = None
    price_alert_below: float | None = None
    alert_on_signals: bool = True
    notes: str | None = None
