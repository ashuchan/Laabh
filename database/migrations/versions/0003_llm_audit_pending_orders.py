"""Add llm_audit_log table and pending_orders table (Phase 1 + Phase 2 gap-fill).

Revision ID: 0003_add_llm_audit_log_and_pending_orders
Revises: 0002_seed_data
Create Date: 2026-04-27
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0003_llm_audit_pending_orders"
down_revision: Union[str, None] = "0002_seed_data"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_audit_log (
            id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            caller          VARCHAR(50) NOT NULL,
            caller_ref_id   UUID,
            model           VARCHAR(50) NOT NULL,
            temperature     NUMERIC(4,2) NOT NULL,
            prompt          TEXT NOT NULL,
            response        TEXT NOT NULL,
            response_parsed JSONB,
            tokens_in       INT,
            tokens_out      INT,
            latency_ms      INT,
            created_at      TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_llm_audit_caller
            ON llm_audit_log(caller, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_llm_audit_caller_ref
            ON llm_audit_log(caller_ref_id);

        CREATE TABLE IF NOT EXISTS pending_orders (
            id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            portfolio_id    UUID NOT NULL REFERENCES portfolios(id),
            instrument_id   UUID NOT NULL REFERENCES instruments(id),
            signal_id       UUID REFERENCES signals(id),
            trade_type      trade_type NOT NULL,
            order_type      order_type NOT NULL,
            quantity        INT NOT NULL,
            limit_price     NUMERIC(12,2),
            trigger_price   NUMERIC(12,2),
            status          VARCHAR(20) DEFAULT 'pending',
            valid_till      TIMESTAMPTZ,
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            executed_at     TIMESTAMPTZ,
            cancelled_at    TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS idx_pending_orders_portfolio
            ON pending_orders(portfolio_id);
        CREATE INDEX IF NOT EXISTS idx_pending_orders_status
            ON pending_orders(status);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS pending_orders CASCADE;
        DROP TABLE IF EXISTS llm_audit_log CASCADE;
        """
    )
