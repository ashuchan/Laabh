"""Seed data — populates default data_sources, NIFTY 50 instruments, watchlists, portfolio.

Revision ID: 0002_seed_data
Revises: 0001_initial_schema
Create Date: 2026-04-15
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence, Union

from alembic import op

revision: str = "0002_seed_data"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SEED_SQL = Path(__file__).parents[2] / "seed.sql"


def upgrade() -> None:
    sql = SEED_SQL.read_text(encoding="utf-8")
    op.execute(sql)


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM watchlist_items;
        DELETE FROM watchlists;
        DELETE FROM portfolios;
        DELETE FROM instruments;
        DELETE FROM data_sources;
        """
    )
