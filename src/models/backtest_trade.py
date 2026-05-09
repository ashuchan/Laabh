"""Backtest trade ledger — one row per trade opened during a backtest run.

Mirrors the shape of ``quant_trades`` (live ledger), with two additions:
  * ``backtest_run_id`` foreign key, linking the trade to its parent run.
  * Provenance columns ``chain_source`` / ``underlying_source`` recording
    which data tier produced the entry/exit prices, so reports can flag
    synthesized vs real fills.

Backtest trades are kept separate from ``quant_trades`` on purpose: mixing
them would distort live analytics and complicate the live-vs-backtest
reconciliation (Task 14).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class BacktestTrade(Base):
    """One trade opened (and closed) during a backtest replay."""

    __tablename__ = "backtest_trades"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    backtest_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("backtest_runs.id"), nullable=False
    )
    underlying_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False
    )

    primitive_name: Mapped[str] = mapped_column(String(30), nullable=False)
    arm_id: Mapped[str] = mapped_column(String(80), nullable=False)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    legs: Mapped[dict] = mapped_column(JSONB, nullable=False)

    entry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_premium_net: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    exit_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    exit_premium_net: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)

    estimated_costs: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)

    signal_strength_at_entry: Mapped[float] = mapped_column(Numeric(5, 3), nullable=False)
    posterior_mean_at_entry: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False)
    sampled_mean_at_entry: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False)
    kelly_fraction: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False)
    lots: Mapped[int] = mapped_column(Integer, nullable=False)

    exit_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)

    # Provenance
    chain_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    underlying_source: Mapped[str | None] = mapped_column(String(20), nullable=True)

    __table_args__ = (
        Index("idx_backtest_trades_run", "backtest_run_id"),
        Index("idx_backtest_trades_arm", "arm_id"),
    )
