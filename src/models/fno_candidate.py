"""F&O candidate rows produced by Phase 1/2/3 filter runs."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class FNOCandidate(Base):
    """Snapshot of one instrument at one phase of the daily filter pipeline."""

    __tablename__ = "fno_candidates"
    __table_args__ = (
        UniqueConstraint("instrument_id", "run_date", "phase"),
        CheckConstraint("phase IN (1,2,3)"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False
    )
    run_date: Mapped[date] = mapped_column(Date, nullable=False)
    phase: Mapped[int] = mapped_column(Integer, nullable=False)

    # Phase 1 outputs
    passed_liquidity: Mapped[bool | None] = mapped_column(Boolean)
    atm_oi: Mapped[int | None] = mapped_column(BigInteger)
    atm_spread_pct: Mapped[float | None] = mapped_column(Numeric(6, 4))
    avg_volume_5d: Mapped[int | None] = mapped_column(BigInteger)

    # Phase 2 outputs
    news_score: Mapped[float | None] = mapped_column(Numeric(4, 2))
    sentiment_score: Mapped[float | None] = mapped_column(Numeric(4, 2))
    fii_dii_score: Mapped[float | None] = mapped_column(Numeric(4, 2))
    macro_align_score: Mapped[float | None] = mapped_column(Numeric(4, 2))
    convergence_score: Mapped[float | None] = mapped_column(Numeric(4, 2))
    composite_score: Mapped[float | None] = mapped_column(Numeric(6, 2))

    # Phase 3 outputs
    technical_pass: Mapped[bool | None] = mapped_column(Boolean)
    iv_regime: Mapped[str | None] = mapped_column(String(15))
    oi_structure: Mapped[str | None] = mapped_column(String(20))
    llm_thesis: Mapped[str | None] = mapped_column(Text)
    llm_decision: Mapped[str | None] = mapped_column(String(10))
    config_version: Mapped[str | None] = mapped_column(String(20))

    # Tier snapshot at write time (migration 0015) — 'T1', 'T2', 'index',
    # or NULL when the instrument isn't in fno_collection_tier. Stored on
    # the row so a backfill of historical run_dates agrees with the tier
    # that was in effect for that day.
    instrument_tier: Mapped[str | None] = mapped_column(String(10))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    dryrun_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
