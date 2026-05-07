"""Eval system tables: agent_predictions_eval, agent_predictions_outcomes, prompt_version_results.

Revision ID: 0010_eval_tables
Revises: 0009_agent_framework
Create Date: 2026-05-07

Second migration for the agentic eval system:
  - agent_predictions_eval: shadow evaluator scores per workflow_run
  - agent_predictions_outcomes: resolved P&L per prediction
  - prompt_version_results: weekly A/B prompt comparison aggregates
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_eval_tables"
down_revision: Union[str, None] = "0009_agent_framework"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
-- ============================================================
-- 1. agent_predictions_eval — shadow evaluator scores
-- ============================================================
CREATE TABLE IF NOT EXISTS agent_predictions_eval (
    id                       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workflow_run_id          UUID NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    prediction_id            UUID REFERENCES agent_predictions(id),
    evaluator_agent_run_id   UUID NOT NULL REFERENCES agent_runs(id),
    evaluator_persona_version TEXT NOT NULL DEFAULT 'v1',

    -- Per-dimension scores (0-10 each)
    calibration_score         NUMERIC(3,1),
    evidence_alignment_score  NUMERIC(3,1),
    guardrail_proximity_score NUMERIC(3,1),
    novelty_score             NUMERIC(3,1),
    self_consistency_score    NUMERIC(3,1),

    -- Computed composite
    overall_score             NUMERIC(3,1) GENERATED ALWAYS AS (
        (calibration_score + evidence_alignment_score + guardrail_proximity_score
         + novelty_score + self_consistency_score) / 5
    ) STORED,

    -- Flags and justifications
    headline_concern          TEXT,
    is_re_skin                BOOLEAN DEFAULT false,
    is_repeat_mistake         BOOLEAN DEFAULT false,
    matched_history_run_ids   JSONB DEFAULT '[]'::jsonb,
    near_misses               JSONB DEFAULT '[]'::jsonb,
    inconsistencies           JSONB DEFAULT '[]'::jsonb,

    -- Cost
    eval_cost_usd             NUMERIC(10,6),
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX agent_predictions_eval_wfr_idx
    ON agent_predictions_eval(workflow_run_id);
CREATE INDEX agent_predictions_eval_score_idx
    ON agent_predictions_eval(overall_score)
    WHERE overall_score < 5;
CREATE INDEX agent_predictions_eval_created_at_idx
    ON agent_predictions_eval(created_at DESC);


-- ============================================================
-- 2. agent_predictions_outcomes — resolved P&L
-- ============================================================
CREATE TABLE IF NOT EXISTS agent_predictions_outcomes (
    id                          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    prediction_id               UUID NOT NULL REFERENCES agent_predictions(id),
    resolved_at                 TIMESTAMPTZ NOT NULL,
    realised_pnl_pct            NUMERIC(8,3) NOT NULL,
    hit_target                  BOOLEAN,
    hit_stop                    BOOLEAN,
    exit_reason                 TEXT,
    exit_price                  NUMERIC(12,4),
    underlying_close_at_resolve NUMERIC(12,4),
    book_at_risk_pct            NUMERIC(6,3),
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (prediction_id)
);

CREATE INDEX agent_predictions_outcomes_resolved_idx
    ON agent_predictions_outcomes(resolved_at DESC);
CREATE INDEX agent_predictions_outcomes_pnl_idx
    ON agent_predictions_outcomes(realised_pnl_pct);


-- ============================================================
-- 3. prompt_version_results — weekly A/B aggregates
-- ============================================================
CREATE TABLE IF NOT EXISTS prompt_version_results (
    id                          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    agent_name                  TEXT NOT NULL,
    candidate_version           TEXT NOT NULL,
    week_iso                    TEXT NOT NULL,        -- e.g. '2026-W18'
    n_replays                   INTEGER NOT NULL,
    n_decisions_changed         INTEGER NOT NULL,
    mean_expected_pnl_delta_pp  NUMERIC(6,3),
    raw_results                 JSONB NOT NULL,
    promotion_recommended       BOOLEAN DEFAULT false,
    promotion_reason            TEXT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (agent_name, candidate_version, week_iso)
);

CREATE INDEX prompt_version_results_promotion_idx
    ON prompt_version_results(promotion_recommended)
    WHERE promotion_recommended = true;
CREATE INDEX prompt_version_results_week_idx
    ON prompt_version_results(week_iso DESC);
"""
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
DROP TABLE IF EXISTS prompt_version_results CASCADE;
DROP TABLE IF EXISTS agent_predictions_outcomes CASCADE;
DROP TABLE IF EXISTS agent_predictions_eval CASCADE;
"""
        )
    )
