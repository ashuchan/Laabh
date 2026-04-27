"""Add chain ingestion observability tables.

Revision ID: 0005_add_chain_ingestion_observability
Revises: 0004_fno_intelligence_module
Create Date: 2026-04-27

Adds:
  - fno_collection_tiers   (per-instrument tier assignment)
  - chain_collection_log   (per-poll outcome log)
  - chain_collection_issues (schema mismatches / sustained failures)
  - source_health          (health status per source)
  - options_chain.source   (provenance column, additive)

Rollback: drops the four new tables and the provenance column.
Existing options_chain data is unaffected.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0005_add_chain_ingestion_observability"
down_revision: Union[str, None] = "0004_fno_intelligence_module"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = """
-- Per-instrument data tier (refreshed daily at 6 AM IST)
CREATE TABLE IF NOT EXISTS fno_collection_tiers (
    instrument_id     UUID PRIMARY KEY REFERENCES instruments(id),
    tier              INT NOT NULL CHECK (tier IN (1, 2)),
    avg_volume_5d     BIGINT,
    last_promoted_at  TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ DEFAULT NOW()
);

-- Per-poll outcome log (one row per underlying per attempted snapshot)
CREATE TABLE IF NOT EXISTS chain_collection_log (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    instrument_id     UUID NOT NULL REFERENCES instruments(id),
    attempted_at      TIMESTAMPTZ NOT NULL,
    primary_source    VARCHAR(20) NOT NULL,
    fallback_source   VARCHAR(20),
    final_source      VARCHAR(20),
    status            VARCHAR(20) NOT NULL CHECK (status IN ('ok','fallback_used','missed')),
    nse_error         TEXT,
    dhan_error        TEXT,
    latency_ms        INT
);
CREATE INDEX IF NOT EXISTS idx_chain_log_attempted
    ON chain_collection_log(attempted_at DESC);
CREATE INDEX IF NOT EXISTS idx_chain_log_status
    ON chain_collection_log(status);

-- Schema mismatches and sustained failures (drives GitHub issue creation)
CREATE TABLE IF NOT EXISTS chain_collection_issues (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source            VARCHAR(20) NOT NULL,
    instrument_id     UUID REFERENCES instruments(id),
    issue_type        VARCHAR(30) NOT NULL
                          CHECK (issue_type IN ('schema_mismatch','sustained_failure','auth_error')),
    error_message     TEXT NOT NULL,
    raw_response      TEXT,
    detected_at       TIMESTAMPTZ DEFAULT NOW(),
    github_issue_url  TEXT,
    resolved_at       TIMESTAMPTZ,
    resolved_by       VARCHAR(50)
);
CREATE INDEX IF NOT EXISTS idx_chain_issues_unresolved
    ON chain_collection_issues(detected_at DESC)
    WHERE resolved_at IS NULL;

-- Source health for the source-pluggable abstraction
CREATE TABLE IF NOT EXISTS source_health (
    source            VARCHAR(20) PRIMARY KEY,
    status            VARCHAR(20) NOT NULL
                          CHECK (status IN ('healthy','degraded','failed')),
    consecutive_errors INT DEFAULT 0,
    last_success_at   TIMESTAMPTZ,
    last_error_at     TIMESTAMPTZ,
    last_error        TEXT,
    updated_at        TIMESTAMPTZ DEFAULT NOW()
);

-- Seed source_health with three rows (idempotent)
INSERT INTO source_health (source, status) VALUES
    ('nse',       'healthy'),
    ('dhan',      'healthy'),
    ('angel_one', 'healthy')
ON CONFLICT (source) DO NOTHING;

-- Add source provenance to existing options_chain
ALTER TABLE options_chain ADD COLUMN IF NOT EXISTS source VARCHAR(20) DEFAULT 'nse';
"""

_DOWNGRADE_SQL = """
ALTER TABLE options_chain DROP COLUMN IF EXISTS source;
DROP TABLE IF EXISTS source_health CASCADE;
DROP TABLE IF EXISTS chain_collection_issues CASCADE;
DROP TABLE IF EXISTS chain_collection_log CASCADE;
DROP TABLE IF EXISTS fno_collection_tiers CASCADE;
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
