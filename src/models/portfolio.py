"""Portfolio, holdings, and daily NAV snapshots."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class Portfolio(Base):
    """A virtual paper-trading portfolio."""

    __tablename__ = "portfolios"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, server_default="Main Portfolio")
    initial_capital: Mapped[float] = mapped_column(Numeric(15, 2), nullable=False, server_default="1000000")
    current_cash: Mapped[float] = mapped_column(Numeric(15, 2), nullable=False, server_default="1000000")

    invested_value: Mapped[float] = mapped_column(Numeric(15, 2), server_default="0")
    current_value: Mapped[float] = mapped_column(Numeric(15, 2), server_default="0")
    total_pnl: Mapped[float] = mapped_column(Numeric(15, 2), server_default="0")
    total_pnl_pct: Mapped[float] = mapped_column(Numeric(8, 4), server_default="0")
    day_pnl: Mapped[float] = mapped_column(Numeric(15, 2), server_default="0")

    benchmark_symbol: Mapped[str] = mapped_column(String(20), server_default="NIFTY 50")
    benchmark_start: Mapped[float | None] = mapped_column(Numeric(12, 2))

    total_trades: Mapped[int] = mapped_column(Integer, server_default="0")
    winning_trades: Mapped[int] = mapped_column(Integer, server_default="0")
    losing_trades: Mapped[int] = mapped_column(Integer, server_default="0")
    win_rate: Mapped[float] = mapped_column(Numeric(5, 4), server_default="0")
    max_drawdown_pct: Mapped[float] = mapped_column(Numeric(8, 4), server_default="0")
    sharpe_ratio: Mapped[float | None] = mapped_column(Numeric(6, 4))

    brokerage_pct: Mapped[float] = mapped_column(Numeric(6, 4), server_default="0.0003")
    stt_pct: Mapped[float] = mapped_column(Numeric(6, 4), server_default="0.001")

    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Holding(Base):
    """Current position in an instrument for a given portfolio."""

    __tablename__ = "holdings"
    __table_args__ = (UniqueConstraint("portfolio_id", "instrument_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("portfolios.id"), nullable=False
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False
    )

    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_buy_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    invested_value: Mapped[float] = mapped_column(Numeric(15, 2), nullable=False)
    current_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    current_value: Mapped[float | None] = mapped_column(Numeric(15, 2))
    pnl: Mapped[float | None] = mapped_column(Numeric(15, 2))
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4))
    day_change_pct: Mapped[float | None] = mapped_column(Numeric(8, 4))
    weight_pct: Mapped[float | None] = mapped_column(Numeric(6, 2))

    first_buy_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_trade_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PortfolioSnapshot(Base):
    """End-of-day NAV snapshot used for performance charting."""

    __tablename__ = "portfolio_snapshots"

    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("portfolios.id"), primary_key=True
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    total_value: Mapped[float | None] = mapped_column(Numeric(15, 2))
    cash: Mapped[float | None] = mapped_column(Numeric(15, 2))
    invested_value: Mapped[float | None] = mapped_column(Numeric(15, 2))
    day_pnl: Mapped[float | None] = mapped_column(Numeric(15, 2))
    day_pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4))
    cumulative_pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4))
    benchmark_value: Mapped[float | None] = mapped_column(Numeric(12, 2))
    benchmark_pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4))
    num_holdings: Mapped[int | None] = mapped_column(Integer)
