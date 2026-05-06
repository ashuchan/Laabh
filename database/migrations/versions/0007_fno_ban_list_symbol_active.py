"""Add symbol and is_active columns to fno_ban_list.

Revision ID: 0007_fno_ban_list_symbol_active
Revises: 0006_add_dryrun_run_id
Create Date: 2026-05-05

The ORM model ``FNOBanList`` ([src/models/fno_ban.py]) declared ``symbol``
(NOT NULL) and ``is_active`` (NOT NULL, default true) columns that were never
added to the database.  Routes selecting from this table — notably
``GET /fno/ban-list`` — fail with ``UndefinedColumnError`` until both columns
exist.

Backfill strategy:
  - ``symbol``: joined from ``instruments.symbol`` via instrument_id.
  - ``is_active``: every existing row defaults to TRUE (the historical
    semantics — rows were inserted only while a stock was on the ban list).

Both columns are added as nullable first, backfilled, then the NOT NULL
constraint is set, so the migration is safe to run on a non-empty table.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_fno_ban_list_symbol_active"
down_revision: Union[str, None] = "0006_add_dryrun_run_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE fno_ban_list ADD COLUMN IF NOT EXISTS symbol VARCHAR(50)"))
    op.execute(
        sa.text(
            """
            UPDATE fno_ban_list b
               SET symbol = i.symbol
              FROM instruments i
             WHERE b.instrument_id = i.id
               AND b.symbol IS NULL
            """
        )
    )
    op.execute(sa.text("ALTER TABLE fno_ban_list ALTER COLUMN symbol SET NOT NULL"))

    op.execute(
        sa.text(
            "ALTER TABLE fno_ban_list ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE fno_ban_list DROP COLUMN IF EXISTS is_active"))
    op.execute(sa.text("ALTER TABLE fno_ban_list DROP COLUMN IF EXISTS symbol"))
