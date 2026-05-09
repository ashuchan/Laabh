"""Backtest run header — one row per (portfolio, backtest_date).

A backtest run captures the configuration, universe, and aggregate result for
replaying one trading day through the orchestrator. Trades produced during
that run are linked via ``backtest_trades.backtest_run_id``.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class BacktestRun(Base):
    """One backtest replay of a single trading day for one portfolio."""

    __tablename__ = "backtest_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("portfolios.id"), nullable=False
    )
    backtest_date: Mapped[date] = mapped_column(Date, nullable=False)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Full QuantSettings snapshot at run time — JSON-serialised by the runner.
    config_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Selected universe for this date — list of {id, symbol} dicts.
    universe: Mapped[list] = mapped_column(JSONB, nullable=False)

    starting_nav: Mapped[float] = mapped_column(Numeric(15, 2), nullable=False)
    final_nav: Mapped[float | None] = mapped_column(Numeric(15, 2), nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Numeric(8, 4), nullable=True)
    trade_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    winning_trades: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Reproducibility: same seed -> bit-identical results (asserted in tests).
    bandit_seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    git_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_backtest_runs_date", "backtest_date"),
        Index("idx_backtest_runs_portfolio_date", "portfolio_id", "backtest_date"),
    )
