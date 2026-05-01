"""Add dryrun_run_id column to F&O write tables.

Revision ID: 0006_add_dryrun_run_id
Revises: 0005_chain_observability
Create Date: 2026-05-01

Adds a nullable UUID column ``dryrun_run_id`` to every table the F&O pipeline
writes to.  Live writes leave the column NULL; replay writes stamp it with the
UUID of the replay invocation so that multiple replays of the same date can
coexist with each other and with live data.

A partial index ``WHERE dryrun_run_id IS NOT NULL`` is created on each table
so that live-path query plans are completely unaffected (the index is only
consulted when filtering by a specific replay run).

Rollback: drops indexes then drops columns. Safe at any time.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0006_add_dryrun_run_id"
down_revision: Union[str, None] = "0005_chain_observability"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Tables that receive the column, in dependency order (parents before children).
_TABLES = [
    "fno_candidates",
    "fno_signals",
    "fno_signal_events",
    "fno_cooldowns",
    "iv_history",
    "vix_ticks",
    "notifications",
    "llm_audit_log",
    "chain_collection_log",
    "options_chain",
    "job_log",
]

_UPGRADE_SQL = "\n".join(
    f"""ALTER TABLE {t} ADD COLUMN IF NOT EXISTS dryrun_run_id UUID NULL;
CREATE INDEX IF NOT EXISTS idx_{t}_dryrun_run_id
    ON {t}(dryrun_run_id)
    WHERE dryrun_run_id IS NOT NULL;"""
    for t in _TABLES
)

_DOWNGRADE_SQL = "\n".join(
    f"""DROP INDEX IF EXISTS idx_{t}_dryrun_run_id;
ALTER TABLE {t} DROP COLUMN IF EXISTS dryrun_run_id;"""
    for t in reversed(_TABLES)
)


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
