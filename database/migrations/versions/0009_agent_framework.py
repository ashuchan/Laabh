"""Agent framework tables: workflow_runs, agent_runs, agent_predictions + llm_audit_log extensions.

Revision ID: 0009_agent_framework
Revises: 0008_strategy_lessons
Create Date: 2026-05-07

Core tables for the agentic workflow system:
  - workflow_runs: one row per predict_today_combined / evaluate_yesterday execution
  - agent_runs: one row per individual agent invocation within a workflow
  - agent_predictions: final allocation decisions produced by the CEO Judge
  - llm_audit_log: extended with caller_tag, caller_meta, request_body, response_body
    for full replay fidelity (agent.* callers need structured request/response, not just
    the legacy prompt/response TEXT columns).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_agent_framework"
down_revision: Union[str, None] = "0008_strategy_lessons"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
-- ============================================================
-- 1. workflow_runs  (one per predict_today_combined execution)
-- ============================================================
CREATE TABLE IF NOT EXISTS workflow_runs (
    id                       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workflow_name            TEXT NOT NULL,
    version                  TEXT NOT NULL DEFAULT 'v1',
    status                   TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running','succeeded','failed','cancelled')),
    status_extended          TEXT
        CHECK (status_extended IN (
            'succeeded','succeeded_with_caveats','failed','cancelled','orphaned'
        )),
    triggered_by             TEXT NOT NULL DEFAULT 'scheduled'
        CHECK (triggered_by IN ('scheduled','manual','replay','shadow_eval')),
    params                   JSONB NOT NULL DEFAULT '{}'::jsonb,
    cost_usd                 NUMERIC(10,6),
    total_tokens             INTEGER,
    error                    TEXT,

    -- Replay / experiment tracking
    parent_run_id            UUID REFERENCES workflow_runs(id),
    experiment_tag           TEXT,
    persona_version_overrides JSONB DEFAULT '{}'::jsonb,

    idempotency_key          TEXT UNIQUE,
    started_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at             TIMESTAMPTZ,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX workflow_runs_status_idx       ON workflow_runs(status);
CREATE INDEX workflow_runs_started_at_idx   ON workflow_runs(started_at DESC);
CREATE INDEX workflow_runs_parent_run_id_idx ON workflow_runs(parent_run_id);
CREATE INDEX workflow_runs_experiment_tag_idx ON workflow_runs(experiment_tag)
    WHERE experiment_tag IS NOT NULL;


-- ============================================================
-- 2. agent_runs  (one per agent invocation inside a workflow)
-- ============================================================
CREATE TABLE IF NOT EXISTS agent_runs (
    id                       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workflow_run_id          UUID NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    agent_name               TEXT NOT NULL,
    persona_version          TEXT NOT NULL DEFAULT 'v1',
    model                    TEXT NOT NULL DEFAULT 'unknown',
    model_used               TEXT,
    status                   TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running','succeeded','skipped','failed','rejected_by_guardrail')),
    output                   JSONB,
    raw_output               JSONB,
    cost_usd                 NUMERIC(10,6),
    input_tokens             INTEGER,
    output_tokens            INTEGER,
    cache_read_tokens        INTEGER DEFAULT 0,
    cache_creation_tokens    INTEGER DEFAULT 0,
    duration_ms              INTEGER,
    error                    TEXT,
    validation_errors        JSONB DEFAULT '[]'::jsonb,
    llm_audit_log_id         UUID REFERENCES llm_audit_log(id),
    iteration_index          INTEGER DEFAULT 0,
    estimated_input_tokens   INTEGER,
    started_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at             TIMESTAMPTZ
);

CREATE INDEX agent_runs_workflow_run_id_idx   ON agent_runs(workflow_run_id);
CREATE INDEX agent_runs_agent_name_status_idx ON agent_runs(agent_name, status);
CREATE INDEX agent_runs_started_at_idx        ON agent_runs(started_at DESC);


-- ============================================================
-- 3. agent_predictions  (final allocation from CEO Judge)
-- ============================================================
CREATE TABLE IF NOT EXISTS agent_predictions (
    id                       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    workflow_run_id          UUID NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    as_of                    TIMESTAMPTZ NOT NULL,

    -- What was decided
    asset_class              TEXT NOT NULL
        CHECK (asset_class IN ('fno','equity','cash')),
    symbol_or_underlying     TEXT NOT NULL,
    decision                 TEXT NOT NULL,     -- BUY/SELL/HOLD or strategy name
    rationale                TEXT,
    conviction               NUMERIC(4,3)
        CHECK (conviction BETWEEN 0 AND 1),
    expected_pnl_pct         NUMERIC(8,3),
    max_loss_pct             NUMERIC(8,3),
    target_price             NUMERIC(14,4),
    stop_price               NUMERIC(14,4),
    horizon                  TEXT,             -- '1d','5d','15d','1m'

    -- Prompt provenance (which version of each agent produced this)
    model_used               TEXT NOT NULL DEFAULT 'unknown',
    prompt_versions          JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Guardrail audit
    guardrail_status         TEXT NOT NULL DEFAULT 'passed',
        -- 'passed' | 'caveat:<validator>' | 'rejected:<validator>'

    -- Kill-switches from Judge (stored for monitoring)
    kill_switches            JSONB DEFAULT '[]'::jsonb,

    -- Full judge output blob
    judge_output             JSONB,

    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX agent_predictions_workflow_run_id_idx ON agent_predictions(workflow_run_id);
CREATE INDEX agent_predictions_as_of_idx           ON agent_predictions(as_of DESC);
CREATE INDEX agent_predictions_symbol_idx          ON agent_predictions(symbol_or_underlying);
CREATE INDEX agent_predictions_guardrail_idx       ON agent_predictions(guardrail_status)
    WHERE guardrail_status != 'passed';


-- ============================================================
-- 4. Extend llm_audit_log for agent.* callers
--    (legacy columns stay for phase1.extractor / fno.thesis)
-- ============================================================
ALTER TABLE llm_audit_log
    ADD COLUMN IF NOT EXISTS caller_tag    TEXT,
    ADD COLUMN IF NOT EXISTS caller_meta   JSONB,
    ADD COLUMN IF NOT EXISTS request_body  JSONB,
    ADD COLUMN IF NOT EXISTS response_body JSONB,
    ADD COLUMN IF NOT EXISTS cache_read_tokens       INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS cache_creation_tokens   INTEGER DEFAULT 0,
    ADD COLUMN IF NOT EXISTS cost_usd      NUMERIC(10,6);

CREATE INDEX IF NOT EXISTS idx_llm_audit_caller_tag ON llm_audit_log(caller_tag)
    WHERE caller_tag IS NOT NULL;
"""
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
DROP TABLE IF EXISTS agent_predictions CASCADE;
DROP TABLE IF EXISTS agent_runs CASCADE;
DROP TABLE IF EXISTS workflow_runs CASCADE;

ALTER TABLE llm_audit_log
    DROP COLUMN IF EXISTS caller_tag,
    DROP COLUMN IF EXISTS caller_meta,
    DROP COLUMN IF EXISTS request_body,
    DROP COLUMN IF EXISTS response_body,
    DROP COLUMN IF EXISTS cache_read_tokens,
    DROP COLUMN IF EXISTS cache_creation_tokens,
    DROP COLUMN IF EXISTS cost_usd;
"""
        )
    )
