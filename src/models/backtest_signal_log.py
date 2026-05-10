"""Per-tick signal-decision log for backtest replays.

One row per ``(tick × signalling arm)``. Captures every primitive output the
orchestrator considered during a backtest, plus a single ``rejection_reason``
classifying *why* the arm did or didn't trade at that tick.

Powers the "missed trades" / selection-funnel report: by joining this table
against ``backtest_runs.universe`` and intraday top-gainers, we can attribute
every missed move to one of five buckets:

  1. Not in universe          (symbol absent from backtest_runs.universe)
  2. In universe, no signal   (symbol present but zero rows here)
  3. Signal too weak          (rejection_reason='weak_signal')
  4. Lost the bandit draw     (rejection_reason='lost_bandit')
  5. Sized to zero            (rejection_reason='sized_zero')

Plus tick-level rejections that aren't symbol-attributable: 'warmup',
'kill_switch', 'capacity_full', 'cooloff'.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class BacktestSignalLog(Base):
    """One signal observation during a backtest tick + its disposition."""

    __tablename__ = "backtest_signal_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    backtest_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("backtest_runs.id"), nullable=False
    )
    virtual_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    underlying_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    arm_id: Mapped[str] = mapped_column(String(80), nullable=False)
    primitive_name: Mapped[str] = mapped_column(String(30), nullable=False)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    strength: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)

    rejection_reason: Mapped[str] = mapped_column(String(20), nullable=False)
    posterior_mean: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 6), nullable=True
    )
    bandit_selected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    lots_sized: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ------------------------------------------------------------------
    # Decision-Inspector trace payloads (PR 1 of the inspector spec).
    # ------------------------------------------------------------------
    # primitive_trace: always populated alongside the row.
    # bandit_trace: set when this arm participated in bandit selection
    #               (lost_bandit / sized_zero / opened); NULL otherwise.
    # sizer_trace: set ONLY on the chosen arm's row.
    #
    # ``none_as_null=True`` makes Python None map to SQL NULL on insert
    # rather than the JSONB ``null`` literal. Without it, downstream
    # queries would have to filter ``WHERE x IS NOT NULL AND
    # jsonb_typeof(x) <> 'null'`` on every read — easy footgun.
    primitive_trace: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    bandit_trace: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    sizer_trace: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )

    __table_args__ = (
        Index("idx_backtest_signal_log_run", "backtest_run_id"),
        Index("idx_backtest_signal_log_run_symbol", "backtest_run_id", "symbol"),
        Index("idx_backtest_signal_log_run_reason", "backtest_run_id", "rejection_reason"),
    )
