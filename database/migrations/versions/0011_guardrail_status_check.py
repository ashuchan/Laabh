"""Add CHECK constraint on agent_predictions.guardrail_status.

Revision ID: 0011_guardrail_status_check
Revises: 0010_eval_tables
Create Date: 2026-05-07

guardrail_status was added in 0009 without a CHECK constraint, leaving the
column accepting arbitrary strings.  The allowed values are:
  'passed'              — all validators passed
  'caveat:<validator>'  — a soft guardrail fired
  'rejected:<validator>'— a hard guardrail fired
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_guardrail_status_check"
down_revision: Union[str, None] = "0010_eval_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
ALTER TABLE agent_predictions
    ADD CONSTRAINT agent_predictions_guardrail_status_check
    CHECK (
        guardrail_status = 'passed'
        OR guardrail_status ~ '^caveat:[A-Za-z_]+$'
        OR guardrail_status ~ '^rejected:[A-Za-z_]+$'
    );
"""
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
ALTER TABLE agent_predictions
    DROP CONSTRAINT IF EXISTS agent_predictions_guardrail_status_check;
"""
        )
    )
