"""Add columns + holdout sentinel table for LLM bootstrap backfill.

Revision ID: 0015_llm_backfill_columns
Revises: 0014_quant_backtest_tables
Create Date: 2026-05-15

Plan reference: docs/llm_feature_generator/backfill_plan.md §8.

Schema deltas:
  * llm_decision_log.propensity_source  — provenance flag for IPS weighting.
      'live'     — real bandit decision (preserved propensity).
      'imputed'  — backfilled with 1/n_arms heuristic; calibration applies
                   an additional 0.3× weight multiplier (same hygiene as
                   counterfactual rows).
      'unknown'  — legacy rows (default for existing data).
  * llm_decision_log.news_cutoff_at     — audit trail of the news cutoff
                                          honored when the prompt was built.
                                          Live rows leave it NULL (no cutoff);
                                          backfill rows stamp ist(D, 9, 0).
  * llm_decision_log.instrument_tier    — T1 / T2 / index hydrated at write
                                          time so calibration can stratify
                                          fits per tier without joining to
                                          fno_collection_tier on every read.
  * llm_calibration_models.holdout_ece  — true out-of-sample ECE on the
                                          reserved holdout window (separate
                                          from the walk-forward cv_ece).
  * llm_calibration_models.holdout_residual_var — paired residual variance.
  * fno_candidates.instrument_tier      — snapshotted tier at Phase 1 time
                                          so a historical replay agrees with
                                          the tier that was in effect on D.
  * backfill_holdout_sentinels (new)    — one row per backfill batch_uuid
                                          recording (holdout_start, end).
                                          Multiple scripts in the same batch
                                          agree on the holdout window via
                                          this sentinel.

Note: the plan also references a `deterministic_universe_snapshot` table.
That artifact already exists in this codebase as `quant_universe_baseline`
(introduced in migration 0013_quant_mode_tables for Phase 0.5). We do NOT
create a duplicate — `quant_universe_baseline` is the single source of
truth and the backfill prereqs script writes to it via
`src.fno.deterministic_universe.run_deterministic_baseline`.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015_llm_backfill_columns"
down_revision: Union[str, None] = "0014_quant_backtest_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # llm_decision_log column additions
    # ------------------------------------------------------------------
    op.add_column(
        "llm_decision_log",
        sa.Column(
            "propensity_source",
            sa.String(20),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column(
        "llm_decision_log",
        sa.Column("news_cutoff_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "llm_decision_log",
        sa.Column("instrument_tier", sa.String(10), nullable=True),
    )

    # ------------------------------------------------------------------
    # llm_calibration_models column additions — holdout-aware fit metrics
    # ------------------------------------------------------------------
    op.add_column(
        "llm_calibration_models",
        sa.Column("holdout_ece", sa.Numeric(8, 6), nullable=True),
    )
    op.add_column(
        "llm_calibration_models",
        sa.Column("holdout_residual_var", sa.Numeric(10, 6), nullable=True),
    )

    # ------------------------------------------------------------------
    # fno_candidates.instrument_tier — snapshotted at write time
    # ------------------------------------------------------------------
    op.add_column(
        "fno_candidates",
        sa.Column("instrument_tier", sa.String(10), nullable=True),
    )

    # ------------------------------------------------------------------
    # backfill_holdout_sentinels — one row per batch
    # ------------------------------------------------------------------
    op.create_table(
        "backfill_holdout_sentinels",
        sa.Column(
            "batch_uuid",
            sa.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("batch_label", sa.String(80), nullable=False),
        sa.Column("holdout_start_date", sa.Date, nullable=False),
        sa.Column("holdout_end_date", sa.Date, nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("backfill_holdout_sentinels")
    op.drop_column("fno_candidates", "instrument_tier")
    op.drop_column("llm_calibration_models", "holdout_residual_var")
    op.drop_column("llm_calibration_models", "holdout_ece")
    op.drop_column("llm_decision_log", "instrument_tier")
    op.drop_column("llm_decision_log", "news_cutoff_at")
    op.drop_column("llm_decision_log", "propensity_source")
