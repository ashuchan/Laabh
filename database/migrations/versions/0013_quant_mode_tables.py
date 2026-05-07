"""Add quant-mode tables: bandit_arm_state, quant_trades, quant_day_state.

Revision ID: 0013_quant_mode_tables
Revises: 0012_raw_content_trigram_idx
Create Date: 2026-05-07

These three tables support the bandit-orchestrated intraday F&O trading mode
(LAABH_INTRADAY_MODE=quant). They are dormant until the flag is flipped.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0013_quant_mode_tables"
down_revision: Union[str, None] = "0012_raw_content_trigram_idx"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";"))

    op.create_table(
        "bandit_arm_state",
        sa.Column("portfolio_id", sa.UUID(as_uuid=True), sa.ForeignKey("portfolios.id"), primary_key=True),
        sa.Column("underlying_id", sa.UUID(as_uuid=True), sa.ForeignKey("instruments.id"), primary_key=True),
        sa.Column("primitive_name", sa.String(30), primary_key=True),
        sa.Column("date", sa.Date, primary_key=True),
        sa.Column("posterior_mean", sa.Numeric(10, 6), nullable=True),
        sa.Column("posterior_var", sa.Numeric(10, 6), nullable=True),
        sa.Column("n_observations", sa.Integer, server_default="0"),
        sa.Column("theta", JSONB, nullable=True),
        sa.Column("a_inv", JSONB, nullable=True),
        sa.Column("b_vector", JSONB, nullable=True),
        sa.Column("last_updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index(
        "idx_bandit_arm_state_recent",
        "bandit_arm_state",
        ["portfolio_id", sa.text("date DESC")],
    )

    op.create_table(
        "quant_trades",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("portfolio_id", sa.UUID(as_uuid=True), sa.ForeignKey("portfolios.id"), nullable=False),
        sa.Column("underlying_id", sa.UUID(as_uuid=True), sa.ForeignKey("instruments.id"), nullable=False),
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
        sa.Column("bandit_seed", sa.BigInteger, nullable=False),
        sa.Column("kelly_fraction", sa.Numeric(6, 4), nullable=False),
        sa.Column("lots", sa.Integer, nullable=False),
        sa.Column("exit_reason", sa.String(30), nullable=True),
        sa.Column("status", sa.String(15), server_default="open"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_quant_trades_portfolio_date", "quant_trades", ["portfolio_id", "entry_at"])
    op.create_index("idx_quant_trades_arm", "quant_trades", ["arm_id", "entry_at"])
    op.create_index("idx_quant_trades_status", "quant_trades", ["status"])

    op.create_table(
        "quant_day_state",
        sa.Column("portfolio_id", sa.UUID(as_uuid=True), sa.ForeignKey("portfolios.id"), primary_key=True),
        sa.Column("date", sa.Date, primary_key=True),
        sa.Column("starting_nav", sa.Numeric(15, 2), nullable=False),
        sa.Column("universe", JSONB, nullable=False),
        sa.Column("lockin_target_pct", sa.Numeric(6, 4), nullable=False),
        sa.Column("kill_switch_pct", sa.Numeric(6, 4), nullable=False),
        sa.Column("lockin_fired_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("kill_switch_fired_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("final_nav", sa.Numeric(15, 2), nullable=True),
        sa.Column("pnl_pct", sa.Numeric(8, 4), nullable=True),
        sa.Column("trade_count", sa.Integer, server_default="0"),
        sa.Column("winning_trades", sa.Integer, server_default="0"),
        sa.Column("bandit_algo", sa.String(15), nullable=False),
        sa.Column("forget_factor", sa.Numeric(5, 3), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()")),
    )


def downgrade() -> None:
    op.drop_table("quant_day_state")
    op.drop_index("idx_quant_trades_status", "quant_trades")
    op.drop_index("idx_quant_trades_arm", "quant_trades")
    op.drop_index("idx_quant_trades_portfolio_date", "quant_trades")
    op.drop_table("quant_trades")
    op.drop_index("idx_bandit_arm_state_recent", "bandit_arm_state")
    op.drop_table("bandit_arm_state")
