"""Watchlist CRUD routes."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from src.api.schemas.watchlist import (
    AddWatchlistItemRequest,
    WatchlistItemResponse,
    WatchlistResponse,
)
from src.db import session_scope
from src.models.watchlist import Watchlist, WatchlistItem

router = APIRouter(prefix="/watchlists", tags=["watchlists"])


@router.get("/", response_model=list[WatchlistResponse])
async def list_watchlists():
    async with session_scope() as session:
        result = await session.execute(select(Watchlist).order_by(Watchlist.created_at))
        return [WatchlistResponse.model_validate(w) for w in result.scalars().all()]


@router.get("/{watchlist_id}", response_model=WatchlistResponse)
async def get_watchlist(watchlist_id: uuid.UUID):
    async with session_scope() as session:
        w = await session.get(Watchlist, watchlist_id)
    if w is None:
        raise HTTPException(status_code=404, detail="Watchlist not found")
    return WatchlistResponse.model_validate(w)


@router.get("/{watchlist_id}/items", response_model=list[WatchlistItemResponse])
async def list_watchlist_items(watchlist_id: uuid.UUID):
    async with session_scope() as session:
        result = await session.execute(
            select(WatchlistItem).where(WatchlistItem.watchlist_id == watchlist_id)
        )
        return [WatchlistItemResponse.model_validate(i) for i in result.scalars().all()]


@router.post("/{watchlist_id}/items", response_model=WatchlistItemResponse, status_code=201)
async def add_watchlist_item(watchlist_id: uuid.UUID, req: AddWatchlistItemRequest):
    item = WatchlistItem(
        watchlist_id=watchlist_id,
        instrument_id=req.instrument_id,
        target_buy_price=req.target_buy_price,
        target_sell_price=req.target_sell_price,
        price_alert_above=req.price_alert_above,
        price_alert_below=req.price_alert_below,
        alert_on_signals=req.alert_on_signals,
        notes=req.notes,
    )
    async with session_scope() as session:
        session.add(item)
        await session.flush()
        await session.refresh(item)
    return WatchlistItemResponse.model_validate(item)


@router.delete("/{watchlist_id}/items/{item_id}", status_code=204)
async def remove_watchlist_item(watchlist_id: uuid.UUID, item_id: uuid.UUID):
    async with session_scope() as session:
        item = await session.get(WatchlistItem, item_id)
        if item is None or item.watchlist_id != watchlist_id:
            raise HTTPException(status_code=404, detail="Item not found")
        await session.delete(item)
