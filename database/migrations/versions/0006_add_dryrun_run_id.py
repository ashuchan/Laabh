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

Tables covered (15):
  fno_candidates, fno_signals, fno_signal_events, fno_cooldowns,
  iv_history, vix_ticks, notifications, llm_audit_log,
  chain_collection_log, options_chain, job_log,
  fno_ban_list, chain_collection_issues, raw_content, fno_collection_tiers.

Tables intentionally NOT covered:
  source_health — replay suppresses writes to it entirely via
  SideEffectGateway (Task 3), so dryrun_run_id tagging is not needed.

TimescaleDB note: options_chain and vix_ticks are hypertables on TimescaleDB
instances.  ``ALTER TABLE … ADD COLUMN`` propagates to all existing chunks via
Timescale's standard DDL handling.  This was verified manually — see
tests/integration/test_migrations.py::test_dryrun_run_id_on_timescale.

Rollback: drops partial indexes then drops columns. Safe at any time;
live path is unaffected throughout.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_add_dryrun_run_id"
down_revision: Union[str, None] = "0005_chain_observability"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The order here is the canonical write order in the pipeline; reversed during
# downgrade for symmetry.  Order doesn't affect correctness — column adds and
# partial-index drops are independent across these tables.
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
    # Added in Task 1B — tables missed from the original spec:
    "fno_ban_list",
    "chain_collection_issues",
    "raw_content",
    "fno_collection_tiers",
]
# Note: source_health intentionally omitted — replay suppresses writes to it
# via SideEffectGateway in Task 3, not via dryrun_run_id tagging.


def upgrade() -> None:
    # Each op.execute call issues a single DDL statement, keeping every
    # operation individually atomic on all supported drivers.
    # ADD COLUMN IF NOT EXISTS preserves idempotency when the column was
    # already added manually (e.g., a partial failed run).
    for t in _TABLES:
        op.execute(sa.text(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS dryrun_run_id UUID"))
        op.create_index(
            f"idx_{t}_dryrun_run_id",
            t,
            ["dryrun_run_id"],
            postgresql_where=sa.text("dryrun_run_id IS NOT NULL"),
            if_not_exists=True,
        )


def downgrade() -> None:
    for t in reversed(_TABLES):
        op.drop_index(f"idx_{t}_dryrun_run_id", table_name=t, if_exists=True)
        op.execute(sa.text(f"ALTER TABLE {t} DROP COLUMN IF EXISTS dryrun_run_id"))
