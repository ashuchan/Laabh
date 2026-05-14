"""ORM model — quant-mode trade ledger (separate from fno_signals)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class QuantTrade(Base):
    """A single trade opened by the bandit orchestrator."""

    __tablename__ = "quant_trades"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("portfolios.id"), nullable=False
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

    exit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_premium_net: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)

    estimated_costs: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)

    signal_strength_at_entry: Mapped[float] = mapped_column(Numeric(5, 3), nullable=False)
    posterior_mean_at_entry: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False)
    sampled_mean_at_entry: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False)
    bandit_seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kelly_fraction: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False)
    lots: Mapped[int] = mapped_column(Integer, nullable=False)

    exit_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    status: Mapped[str] = mapped_column(String(15), server_default="open")

    # Entry-time LinTS context vector (list of floats). Used by the update
    # path so the bandit learns against the same context it sampled from at
    # selection. NULL on rows opened before the column was added, in which
    # case the update degrades to a zero-context no-op (legacy behaviour).
    entry_context: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("idx_quant_trades_portfolio_date", "portfolio_id", "entry_at"),
        Index("idx_quant_trades_arm", "arm_id", "entry_at"),
        Index("idx_quant_trades_status", "status"),
    )
