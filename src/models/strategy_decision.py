"""LLM strategy-decision audit log — one row per morning/intraday/EOD call."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class StrategyDecision(Base):
    """Audit row capturing one LLM strategy invocation and its actions.

    Powers replay, daily-summary explainability, and post-hoc analysis of
    whether the brain's reasoning matched the outcome.
    """

    __tablename__ = "strategy_decisions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    portfolio_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("portfolios.id"), nullable=False
    )
    decision_type: Mapped[str] = mapped_column(String(40), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    risk_profile: Mapped[str | None] = mapped_column(String(20))
    budget_available: Mapped[float | None] = mapped_column(Numeric(15, 2))
    input_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    llm_model: Mapped[str | None] = mapped_column(String(80))
    llm_reasoning: Mapped[str | None] = mapped_column(Text)
    actions_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    actions_executed: Mapped[int] = mapped_column(Integer, server_default="0")
    actions_skipped: Mapped[int] = mapped_column(Integer, server_default="0")
    dryrun_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
