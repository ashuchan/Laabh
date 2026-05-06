"""Add strategy_lessons table — versioned post-mortem feed for prompt enrichment.

Revision ID: 0008_strategy_lessons
Revises: 0007_fno_ban_list_symbol_active
Create Date: 2026-05-05

Stores short, actionable lessons surfaced from past trading sessions.
The equity strategist and F&O thesis prompts pull recent active rows so the
LLM has its own track record + named failure modes in context. Lessons are
appended (not edited) so the audit trail is preserved; flip ``is_active`` to
retire one without losing history.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_strategy_lessons"
down_revision: Union[str, None] = "0007_fno_ban_list_symbol_active"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            CREATE TABLE IF NOT EXISTS strategy_lessons (
                id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                asset_class  VARCHAR(20) NOT NULL,
                lesson_date  DATE NOT NULL,
                severity     VARCHAR(10) NOT NULL,
                title        VARCHAR(200) NOT NULL,
                body         TEXT NOT NULL,
                is_active    BOOLEAN NOT NULL DEFAULT TRUE,
                created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_strategy_lessons_active "
            "ON strategy_lessons (asset_class, lesson_date DESC) "
            "WHERE is_active"
        )
    )

    seed_rows = [
        (
            "FNO",
            "2026-05-05",
            "blocking",
            "Wide naked-long basket in high-VIX gap-down loses",
            "On 2026-05-05 the F&O book opened 18 long_calls at 09:15 into VIX 18.46, "
            "a gap-down Gift Nifty, Brent>$110 and INR at a record low. Net P&L on the "
            "basket was only +Rs165 across 18 names while the stops on a separate batch "
            "lost Rs52,375. Long premium decays in high IV without follow-through; a "
            "basket of single-leg longs in a falling tape is a synthetic short-vol bet "
            "with extra brokerage. When VIX>=17 and Gift Nifty signals risk-off, prefer "
            "debit spreads or skip; never blanket-cover.",
        ),
        (
            "FNO",
            "2026-05-05",
            "blocking",
            "Stop_premium near zero is not a stop, it is full decay",
            "ADANIPORTS long_call hit a stop_premium_net of Rs41.01 against an entry of "
            "Rs29,309 (99.86% premium loss) and booked -Rs29,295. Stops at sub-1% of "
            "entry premium are mislabelled hold-to-expiry trades. Long-option stops "
            "should sit at no worse than 45% premium drawdown of the entry net cost, "
            "or be expressed as a level on the underlying translated through delta.",
        ),
        (
            "FNO",
            "2026-05-05",
            "major",
            "Opposing or duplicate legs paid theta on both sides",
            "DRREDDY had a long_put open from day-1 while a fresh long_call was proposed "
            "on the same underlying same expiry — net synthetic-flat, paying theta both "
            "ways. MPHASIS, BEL and LT each had duplicate long_calls from prior session "
            "and today's batch. The 10:30 cleanup cost Rs21,300. Always read the open "
            "F&O book before proposing: reject same-strategy duplicates and opposing "
            "directional legs on the same underlying/expiry.",
        ),
        (
            "EQUITY",
            "2026-05-05",
            "blocking",
            "Costs ate 142% of gross when round-tripping at sub-scale",
            "On 2026-05-05 five intraday round-trips produced gross +Rs16.50 and net "
            "-Rs7.01 — costs (brokerage+STT) of Rs23.51 consumed the entire edge. At "
            "Rs40K capital with 1-13 share ticket sizes, costs alone are 1-2.5% per "
            "round-trip. Rule: if expected move (target-entry)/entry < 2 * cost_pct, "
            "do not take the trade. Either upsize the paper book or wait for >2% setups.",
        ),
        (
            "EQUITY",
            "2026-05-05",
            "major",
            "High-VIX EOD blanket-flatten contradicts entries",
            "Balanced + high-VIX rule closed CIPLA (-0.36%), ONGC (-1.04%), SHRIRAMFIN "
            "(+0.44%) at 15:20 regardless of conviction — three of those were 4-source "
            "convergence entries booked 5 hours earlier. ADANIPORTS (+1.48% in 33 min) "
            "was closed pre-open under the same rule and ran. If high-VIX disqualifies "
            "overnight holds it should also disqualify the entry; do not enter-then-"
            "flatten the same day. Strong-conviction (>=0.80) entries with multi-"
            "session catalysts should be allowed to ride with a -1.5% stop.",
        ),
    ]

    for asset, lesson_date, severity, title, body in seed_rows:
        op.execute(
            sa.text(
                """
                INSERT INTO strategy_lessons
                    (asset_class, lesson_date, severity, title, body)
                SELECT :asset, :ld, :sev, :title, :body
                 WHERE NOT EXISTS (
                    SELECT 1 FROM strategy_lessons
                     WHERE title = :title AND lesson_date = :ld
                 )
                """
            ).bindparams(
                asset=asset, ld=lesson_date, sev=severity, title=title, body=body,
            )
        )


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS idx_strategy_lessons_active"))
    op.execute(sa.text("DROP TABLE IF EXISTS strategy_lessons"))
