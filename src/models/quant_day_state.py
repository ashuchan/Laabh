"""ORM model — per-day quant session state."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class QuantDayState(Base):
    """Snapshot of quant-mode session state for one portfolio + trading day."""

    __tablename__ = "quant_day_state"

    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("portfolios.id"), primary_key=True
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)

    starting_nav: Mapped[float] = mapped_column(Numeric(15, 2), nullable=False)
    universe: Mapped[dict] = mapped_column(JSONB, nullable=False)
    lockin_target_pct: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False)
    kill_switch_pct: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False)

    lockin_fired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    kill_switch_fired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    final_nav: Mapped[float | None] = mapped_column(Numeric(15, 2), nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    trade_count: Mapped[int] = mapped_column(Integer, server_default="0")
    winning_trades: Mapped[int] = mapped_column(Integer, server_default="0")

    bandit_algo: Mapped[str] = mapped_column(String(15), nullable=False)
    forget_factor: Mapped[float] = mapped_column(Numeric(5, 3), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
