"""Add pg_trgm GIN index on raw_content.content_text for ILIKE symbol search.

Revision ID: 0012_raw_content_trigram_idx
Revises: 0011_guardrail_status_check
Create Date: 2026-05-07

The news.py SQL executor uses ILIKE '%<symbol>%' on raw_content.content_text to
match analyst commentary.  Without an index, every news_finder call causes a
full-table scan on raw_content.  At 200+ watchlist instruments × 7-day windows
this becomes the hot query path.

This migration enables pg_trgm and adds a GIN trigram index so ILIKE queries
can use index scans instead.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_raw_content_trigram_idx"
down_revision: Union[str, None] = "0011_guardrail_status_check"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm;"))
    op.execute(
        sa.text(
            """
CREATE INDEX IF NOT EXISTS idx_raw_content_content_text_trgm
    ON raw_content USING GIN (content_text gin_trgm_ops);
"""
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("DROP INDEX IF EXISTS idx_raw_content_content_text_trgm;")
    )
