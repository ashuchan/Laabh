"""SQLAlchemy ORM model for agent_predictions table."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Numeric, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class AgentPrediction(Base):
    """Final allocation decision produced by the CEO Judge for one workflow_run."""

    __tablename__ = "agent_predictions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.uuid_generate_v4()
    )
    workflow_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    asset_class: Mapped[str] = mapped_column(Text, nullable=False)
    symbol_or_underlying: Mapped[str] = mapped_column(Text, nullable=False)
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    conviction: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    expected_pnl_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    max_loss_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    horizon: Mapped[str | None] = mapped_column(Text)

    model_used: Mapped[str] = mapped_column(Text, nullable=False, server_default="unknown")
    prompt_versions: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")

    guardrail_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="passed")
    kill_switches: Mapped[list] = mapped_column(JSONB, server_default="[]")
    judge_output: Mapped[dict | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
