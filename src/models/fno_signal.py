"""F&O signal model — strike-level option trade recommendations."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class FNOSignal(Base):
    """A proposed or active F&O paper trade with full lifecycle tracking."""

    __tablename__ = "fno_signals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    underlying_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False
    )
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fno_candidates.id")
    )

    strategy_type: Mapped[str] = mapped_column(String(20), nullable=False)
    expiry_date: Mapped[date] = mapped_column(Date, nullable=False)
    legs: Mapped[dict] = mapped_column(JSONB, nullable=False)

    entry_premium_net: Mapped[float | None] = mapped_column(Numeric(12, 2))
    target_premium_net: Mapped[float | None] = mapped_column(Numeric(12, 2))
    stop_premium_net: Mapped[float | None] = mapped_column(Numeric(12, 2))
    max_loss: Mapped[float | None] = mapped_column(Numeric(12, 2))
    max_profit: Mapped[float | None] = mapped_column(Numeric(12, 2))
    breakeven_price: Mapped[float | None] = mapped_column(Numeric(12, 2))

    ranker_score: Mapped[float | None] = mapped_column(Numeric(6, 2))
    ranker_breakdown: Mapped[dict | None] = mapped_column(JSONB)
    ranker_version: Mapped[str | None] = mapped_column(String(20))

    iv_regime_at_entry: Mapped[str | None] = mapped_column(String(15))
    vix_at_entry: Mapped[float | None] = mapped_column(Numeric(8, 4))
    # Best-available annualised realised vol at entry — outcome attribution
    # divides by this to get outcome_z (plan §0.3). The writer prefers
    # feature_store.rv_30min when intraday bars exist; falls back to
    # iv_history.rv_20d otherwise. NULL on legacy rows opened before this
    # column existed (attribution then re-derives at read time).
    rv_annualised_at_entry: Mapped[float | None] = mapped_column(Numeric(8, 4))

    status: Mapped[str] = mapped_column(String(15), server_default="proposed")
    proposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    final_pnl: Mapped[float | None] = mapped_column(Numeric(12, 2))
    notes: Mapped[str | None] = mapped_column(Text)
    dryrun_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class FNOSignalEvent(Base):
    """Append-only audit trail for every FNOSignal status transition."""

    __tablename__ = "fno_signal_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    signal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fno_signals.id"), nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(String(15))
    to_status: Mapped[str] = mapped_column(String(15), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    dryrun_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
