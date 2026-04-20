"""Watchlists and watchlist items."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class Watchlist(Base):
    """A named list of instruments the user is watching."""

    __tablename__ = "watchlists"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_default: Mapped[bool] = mapped_column(Boolean, server_default="false")
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WatchlistItem(Base):
    """An instrument in a watchlist, with per-user price alerts and notes."""

    __tablename__ = "watchlist_items"
    __table_args__ = (UniqueConstraint("watchlist_id", "instrument_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    watchlist_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("watchlists.id", ondelete="CASCADE"), nullable=False
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False
    )

    price_alert_above: Mapped[float | None] = mapped_column(Numeric(12, 2))
    price_alert_below: Mapped[float | None] = mapped_column(Numeric(12, 2))
    alert_on_news: Mapped[bool] = mapped_column(Boolean, server_default="true")
    alert_on_signals: Mapped[bool] = mapped_column(Boolean, server_default="true")

    notes: Mapped[str | None] = mapped_column(Text)
    target_buy_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    target_sell_price: Mapped[float | None] = mapped_column(Numeric(12, 2))

    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
