"""Add quant-mode backtest tables.

Revision ID: 0014_quant_backtest_tables
Revises: 0013_quant_mode_tables
Create Date: 2026-05-09

Adds four tables for the real-data backtest harness (CLAUDE-FNO-TASK-QUANT-BACKTEST):

  * price_intraday       — 3-min OHLCV bars for the F&O universe (TimescaleDB hypertable)
  * rbi_repo_history     — daily RBI repo rate, used as risk-free rate in BS pricing
  * backtest_runs        — one row per (portfolio, backtest_date)
  * backtest_trades      — per-trade ledger for backtests, mirrors quant_trades shape

Backtest tables are kept separate from live (quant_trades, quant_day_state) on
purpose: mixing them would distort live analytics and complicate the
"live vs backtest" reconciliation in Task 14.

Decision Notes:
  * price_intraday uses (instrument_id, timestamp) PK to match the project
    convention established by price_ticks. Hypertable on `timestamp` with
    7-day chunk interval (same as price_ticks).
  * backtest_trades has *no* `status` column (unlike quant_trades) — every
    backtest trade is closed by EOD by the runner, so `status='open'` would
    never appear. Provenance columns (chain_source, underlying_source) record
    which data tier was used so reports can flag synthesized vs real fills.
  * backtest_runs.bandit_seed is BIGINT to match quant_trades.bandit_seed.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0014_quant_backtest_tables"
down_revision: Union[str, None] = "0013_quant_mode_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";'))

    # ------------------------------------------------------------------
    # price_intraday — 3-min OHLCV bars for F&O universe
    # ------------------------------------------------------------------
    op.create_table(
        "price_intraday",
        sa.Column(
            "instrument_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("instruments.id"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "timestamp",
            sa.TIMESTAMP(timezone=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("open", sa.Numeric(12, 2), nullable=False),
        sa.Column("high", sa.Numeric(12, 2), nullable=False),
        sa.Column("low", sa.Numeric(12, 2), nullable=False),
        sa.Column("close", sa.Numeric(12, 2), nullable=False),
        sa.Column("volume", sa.BigInteger, nullable=False),
        sa.Column("vwap", sa.Numeric(12, 2), nullable=True),
    )
    # Convert to hypertable when TimescaleDB is available — fall back silently otherwise.
    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                PERFORM create_hypertable('price_intraday', 'timestamp',
                    chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE);
            EXCEPTION WHEN others THEN
                RAISE NOTICE 'TimescaleDB unavailable — price_intraday is a plain table';
            END; $$;
            """
        )
    )
    op.create_index(
        "idx_price_intraday_recent",
        "price_intraday",
        ["instrument_id", sa.text("timestamp DESC")],
    )

    # ------------------------------------------------------------------
    # rbi_repo_history — risk-free rate, daily
    # ------------------------------------------------------------------
    op.create_table(
        "rbi_repo_history",
        sa.Column("date", sa.Date, primary_key=True, nullable=False),
        sa.Column("repo_rate_pct", sa.Numeric(6, 4), nullable=False),
        sa.Column("source", sa.String(30), nullable=True),
        sa.Column(
            "loaded_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("NOW()"),
        ),
    )

    # ------------------------------------------------------------------
    # backtest_runs — header per (portfolio, backtest_date)
    # ------------------------------------------------------------------
    op.create_table(
        "backtest_runs",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column(
            "portfolio_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("portfolios.id"),
            nullable=False,
        ),
        sa.Column("backtest_date", sa.Date, nullable=False),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("NOW()"),
        ),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("config_snapshot", JSONB, nullable=False),
        sa.Column("universe", JSONB, nullable=False),
        sa.Column("starting_nav", sa.Numeric(15, 2), nullable=False),
        sa.Column("final_nav", sa.Numeric(15, 2), nullable=True),
        sa.Column("pnl_pct", sa.Numeric(8, 4), nullable=True),
        sa.Column("trade_count", sa.Integer, nullable=True),
        sa.Column("winning_trades", sa.Integer, nullable=True),
        sa.Column("bandit_seed", sa.BigInteger, nullable=False),
        sa.Column("git_sha", sa.String(40), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
    )
    op.create_index(
        "idx_backtest_runs_date",
        "backtest_runs",
        [sa.text("backtest_date DESC")],
    )
    # Compound index on (portfolio_id, backtest_date) — the compare-tool
    # (scripts/backtest_compare_to_paper.py) and any per-portfolio analytics
    # filter on portfolio_id and order by date.
    op.create_index(
        "idx_backtest_runs_portfolio_date",
        "backtest_runs",
        ["portfolio_id", sa.text("backtest_date DESC")],
    )

    # ------------------------------------------------------------------
    # backtest_trades — per-trade ledger
    # ------------------------------------------------------------------
    op.create_table(
        "backtest_trades",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column(
            "backtest_run_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("backtest_runs.id"),
            nullable=False,
        ),
        sa.Column(
            "underlying_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("instruments.id"),
            nullable=False,
        ),
        sa.Column("primitive_name", sa.String(30), nullable=False),
        sa.Column("arm_id", sa.String(80), nullable=False),
        sa.Column("direction", sa.String(20), nullable=False),
        sa.Column("legs", JSONB, nullable=False),
        sa.Column("entry_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("entry_premium_net", sa.Numeric(12, 2), nullable=False),
        sa.Column("exit_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("exit_premium_net", sa.Numeric(12, 2), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(12, 2), nullable=True),
        sa.Column("estimated_costs", sa.Numeric(10, 2), nullable=False),
        sa.Column("signal_strength_at_entry", sa.Numeric(5, 3), nullable=False),
        sa.Column("posterior_mean_at_entry", sa.Numeric(10, 6), nullable=False),
        sa.Column("sampled_mean_at_entry", sa.Numeric(10, 6), nullable=False),
        sa.Column("kelly_fraction", sa.Numeric(6, 4), nullable=False),
        sa.Column("lots", sa.Integer, nullable=False),
        sa.Column("exit_reason", sa.String(30), nullable=True),
        # Provenance — which data tier produced this trade's premium / underlying
        sa.Column("chain_source", sa.String(20), nullable=True),
        sa.Column("underlying_source", sa.String(20), nullable=True),
    )
    op.create_index("idx_backtest_trades_run", "backtest_trades", ["backtest_run_id"])
    op.create_index("idx_backtest_trades_arm", "backtest_trades", ["arm_id"])


def downgrade() -> None:
    op.drop_index("idx_backtest_trades_arm", "backtest_trades")
    op.drop_index("idx_backtest_trades_run", "backtest_trades")
    op.drop_table("backtest_trades")

    op.drop_index("idx_backtest_runs_portfolio_date", "backtest_runs")
    op.drop_index("idx_backtest_runs_date", "backtest_runs")
    op.drop_table("backtest_runs")

    op.drop_table("rbi_repo_history")

    op.drop_index("idx_price_intraday_recent", "price_intraday")
    op.drop_table("price_intraday")
