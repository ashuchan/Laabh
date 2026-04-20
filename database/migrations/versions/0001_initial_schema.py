"""Initial schema — loads schema.sql (DDL with TimescaleDB, enums, views, functions).

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-04-15
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence, Union

from alembic import op

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA_SQL = Path(__file__).parents[2] / "schema.sql"


def upgrade() -> None:
    sql = SCHEMA_SQL.read_text(encoding="utf-8")
    op.execute(sql)


def downgrade() -> None:
    op.execute(
        """
        DROP VIEW IF EXISTS watchlist_live CASCADE;
        DROP VIEW IF EXISTS portfolio_overview CASCADE;
        DROP VIEW IF EXISTS active_signals_view CASCADE;
        DROP VIEW IF EXISTS analyst_leaderboard CASCADE;
        DROP FUNCTION IF EXISTS resolve_expired_signals() CASCADE;
        DROP FUNCTION IF EXISTS update_analyst_scores() CASCADE;
        DROP TABLE IF EXISTS signal_auto_trades CASCADE;
        DROP TABLE IF EXISTS job_log CASCADE;
        DROP TABLE IF EXISTS system_config CASCADE;
        DROP TABLE IF EXISTS market_sentiment CASCADE;
        DROP TABLE IF EXISTS transcript_chunks CASCADE;
        DROP TABLE IF EXISTS transcription_jobs CASCADE;
        DROP TABLE IF EXISTS notifications CASCADE;
        DROP TABLE IF EXISTS portfolio_snapshots CASCADE;
        DROP TABLE IF EXISTS holdings CASCADE;
        DROP TABLE IF EXISTS trades CASCADE;
        DROP TABLE IF EXISTS portfolios CASCADE;
        DROP TABLE IF EXISTS watchlist_items CASCADE;
        DROP TABLE IF EXISTS watchlists CASCADE;
        DROP TABLE IF EXISTS signals CASCADE;
        DROP TABLE IF EXISTS analysts CASCADE;
        DROP TABLE IF EXISTS raw_content CASCADE;
        DROP TABLE IF EXISTS data_sources CASCADE;
        DROP TABLE IF EXISTS price_daily CASCADE;
        DROP TABLE IF EXISTS price_ticks CASCADE;
        DROP TABLE IF EXISTS instruments CASCADE;
        DROP TYPE IF EXISTS market_segment CASCADE;
        DROP TYPE IF EXISTS transcription_status CASCADE;
        DROP TYPE IF EXISTS notification_priority CASCADE;
        DROP TYPE IF EXISTS notification_type CASCADE;
        DROP TYPE IF EXISTS order_type CASCADE;
        DROP TYPE IF EXISTS trade_status CASCADE;
        DROP TYPE IF EXISTS trade_type CASCADE;
        DROP TYPE IF EXISTS signal_status CASCADE;
        DROP TYPE IF EXISTS signal_timeframe CASCADE;
        DROP TYPE IF EXISTS signal_action CASCADE;
        DROP TYPE IF EXISTS source_status CASCADE;
        DROP TYPE IF EXISTS source_type CASCADE;
        """
    )
