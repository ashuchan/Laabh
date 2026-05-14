"""ORM model for llm_decision_log and llm_calibration_models.

Created by migration ``database/migrations/2026_05_15_llm_features.sql``.
Plan reference: docs/llm_feature_generator/implementation_plan.md §0.1.

One row per (run_date, instrument_id, phase, prompt_version, dryrun_run_id)
LLM call. The row evolves over its lifetime:

  1. Written synchronously at decision time with the raw payload and the
     bandit context (posterior_mean / var / propensity) — outcome columns
     are NULL.
  2. Phase 2 fills calibrated_conviction + calibration_model_id when the
     weekly calibration job activates a new model.
  3. The outcome-attribution job (Phase 0.3) fills outcome_pnl_pct,
     outcome_z, outcome_class, and outcome_attributed_at when the
     downstream fno_signal closes (or counterfactual / timeout fires).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class LLMDecisionLog(Base):
    """One row per LLM call captured for calibration + outcome attribution."""

    __tablename__ = "llm_decision_log"
    __table_args__ = (
        UniqueConstraint(
            "run_date", "instrument_id", "phase", "prompt_version", "dryrun_run_id"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_date: Mapped[date] = mapped_column(Date, nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dryrun_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False
    )
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[str] = mapped_column(Text, nullable=False)

    # Legacy categorical (v9 fills this; v10 leaves NULL).
    decision_label: Mapped[str | None] = mapped_column(Text)

    # Continuous features — populated by v10 prompt.
    directional_conviction: Mapped[float | None] = mapped_column(Numeric(8, 6))
    thesis_durability: Mapped[float | None] = mapped_column(Numeric(8, 6))
    catalyst_specificity: Mapped[float | None] = mapped_column(Numeric(8, 6))
    risk_flag: Mapped[float | None] = mapped_column(Numeric(8, 6))
    raw_confidence: Mapped[float | None] = mapped_column(Numeric(8, 6))

    # Calibrated values — populated by Phase 2 when a model is active.
    calibrated_conviction: Mapped[float | None] = mapped_column(Numeric(8, 6))
    calibration_model_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("llm_calibration_models.id", ondelete="SET NULL")
    )

    # Outcome — filled on position close (or counterfactual / timeout).
    outcome_pnl_pct: Mapped[float | None] = mapped_column(Numeric(10, 6))
    outcome_z: Mapped[float | None] = mapped_column(Numeric(10, 6))
    outcome_class: Mapped[str | None] = mapped_column(Text)
    bandit_posterior_mean: Mapped[float | None] = mapped_column(Numeric(10, 6))
    bandit_posterior_var: Mapped[float | None] = mapped_column(Numeric(10, 6))
    bandit_arm_propensity: Mapped[float | None] = mapped_column(Numeric(10, 6))
    outcome_attributed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Full LLM payload (parsed JSON or {'raw_text': ...} fallback).
    raw_response: Mapped[dict] = mapped_column(JSONB, nullable=False)


class LLMCalibrationModel(Base):
    """One row per fitted calibration curve. Many fits, one active per key."""

    __tablename__ = "llm_calibration_models"
    __table_args__ = (
        UniqueConstraint(
            "prompt_version", "phase", "feature_name", "instrument_tier", "fitted_at"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    feature_name: Mapped[str] = mapped_column(Text, nullable=False)
    instrument_tier: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    n_observations: Mapped[int] = mapped_column(Integer, nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False)
    cv_ece: Mapped[float | None] = mapped_column(Numeric(8, 6))
    cv_residual_var: Mapped[float | None] = mapped_column(Numeric(10, 6))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="false")


class QuantUniverseBaseline(Base):
    """Phase 0.5 deterministic six-factor top-K snapshot per run_date."""

    __tablename__ = "quant_universe_baseline"
    __table_args__ = (
        UniqueConstraint("run_date", "instrument_id", "dryrun_run_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_date: Mapped[date] = mapped_column(Date, nullable=False)
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    composite_score: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False)
    z_liquidity: Mapped[float | None] = mapped_column(Numeric(10, 6))
    z_iv_rank_momentum: Mapped[float | None] = mapped_column(Numeric(10, 6))
    z_rv_regime: Mapped[float | None] = mapped_column(Numeric(10, 6))
    z_trend_strength: Mapped[float | None] = mapped_column(Numeric(10, 6))
    z_mean_reversion: Mapped[float | None] = mapped_column(Numeric(10, 6))
    z_microstructure: Mapped[float | None] = mapped_column(Numeric(10, 6))
    composite_version: Mapped[str] = mapped_column(String(20), nullable=False, server_default="v0_equal")
    dryrun_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
