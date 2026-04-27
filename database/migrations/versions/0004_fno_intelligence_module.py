"""Add F&O intelligence module tables.

Revision ID: 0004_fno_intelligence_module
Revises: 0003_llm_audit_pending_orders
Create Date: 2026-04-27
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0004_fno_intelligence_module"
down_revision: Union[str, None] = "0003_llm_audit_pending_orders"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = """
CREATE TABLE IF NOT EXISTS fno_ban_list (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    instrument_id   UUID NOT NULL REFERENCES instruments(id),
    ban_date        DATE NOT NULL,
    source          VARCHAR(20) DEFAULT 'NSE',
    fetched_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (instrument_id, ban_date, source)
);
CREATE INDEX IF NOT EXISTS idx_fno_ban_date ON fno_ban_list(ban_date);

CREATE TABLE IF NOT EXISTS options_chain (
    instrument_id     UUID NOT NULL REFERENCES instruments(id),
    snapshot_at       TIMESTAMPTZ NOT NULL,
    expiry_date       DATE NOT NULL,
    strike_price      NUMERIC(12,2) NOT NULL,
    option_type       CHAR(2) NOT NULL CHECK (option_type IN ('CE','PE')),
    ltp               NUMERIC(12,2),
    bid_price         NUMERIC(12,2),
    ask_price         NUMERIC(12,2),
    bid_qty           INT,
    ask_qty           INT,
    volume            BIGINT,
    oi                BIGINT,
    oi_change         BIGINT,
    iv                NUMERIC(8,4),
    delta             NUMERIC(8,4),
    gamma             NUMERIC(10,6),
    theta             NUMERIC(10,4),
    vega              NUMERIC(10,4),
    underlying_ltp    NUMERIC(12,2),
    PRIMARY KEY (instrument_id, snapshot_at, expiry_date, strike_price, option_type)
);
DO $$ BEGIN
    PERFORM create_hypertable('options_chain','snapshot_at',
        chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
EXCEPTION WHEN others THEN
    RAISE NOTICE 'TimescaleDB unavailable — options_chain plain table';
END; $$;
CREATE INDEX IF NOT EXISTS idx_options_chain_underlying_expiry
    ON options_chain(instrument_id, expiry_date, snapshot_at DESC);

CREATE TABLE IF NOT EXISTS iv_history (
    instrument_id     UUID NOT NULL REFERENCES instruments(id),
    date              DATE NOT NULL,
    atm_iv            NUMERIC(8,4) NOT NULL,
    iv_rank_52w       NUMERIC(6,2),
    iv_percentile_52w NUMERIC(6,2),
    PRIMARY KEY (instrument_id, date)
);

CREATE TABLE IF NOT EXISTS vix_ticks (
    timestamp         TIMESTAMPTZ NOT NULL,
    vix_value         NUMERIC(8,4) NOT NULL,
    regime            VARCHAR(10) NOT NULL CHECK (regime IN ('low','neutral','high')),
    PRIMARY KEY (timestamp)
);
DO $$ BEGIN
    PERFORM create_hypertable('vix_ticks','timestamp',
        chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE);
EXCEPTION WHEN others THEN
    RAISE NOTICE 'TimescaleDB unavailable — vix_ticks plain table';
END; $$;

CREATE TABLE IF NOT EXISTS fno_candidates (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    instrument_id     UUID NOT NULL REFERENCES instruments(id),
    run_date          DATE NOT NULL,
    phase             INT NOT NULL CHECK (phase IN (1,2,3)),
    passed_liquidity  BOOLEAN,
    atm_oi            BIGINT,
    atm_spread_pct    NUMERIC(6,4),
    avg_volume_5d     BIGINT,
    news_score        NUMERIC(4,2),
    sentiment_score   NUMERIC(4,2),
    fii_dii_score     NUMERIC(4,2),
    macro_align_score NUMERIC(4,2),
    convergence_score NUMERIC(4,2),
    composite_score   NUMERIC(6,2),
    technical_pass    BOOLEAN,
    iv_regime         VARCHAR(15),
    oi_structure      VARCHAR(20),
    llm_thesis        TEXT,
    llm_decision      VARCHAR(10),
    config_version    VARCHAR(20),
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (instrument_id, run_date, phase)
);
CREATE INDEX IF NOT EXISTS idx_fno_candidates_run
    ON fno_candidates(run_date, phase, composite_score DESC);

CREATE TABLE IF NOT EXISTS fno_signals (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    underlying_id       UUID NOT NULL REFERENCES instruments(id),
    candidate_id        UUID REFERENCES fno_candidates(id),
    strategy_type       VARCHAR(20) NOT NULL,
    expiry_date         DATE NOT NULL,
    legs                JSONB NOT NULL,
    entry_premium_net   NUMERIC(12,2),
    target_premium_net  NUMERIC(12,2),
    stop_premium_net    NUMERIC(12,2),
    max_loss            NUMERIC(12,2),
    max_profit          NUMERIC(12,2),
    breakeven_price     NUMERIC(12,2),
    ranker_score        NUMERIC(6,2),
    ranker_breakdown    JSONB,
    ranker_version      VARCHAR(20),
    iv_regime_at_entry  VARCHAR(15),
    vix_at_entry        NUMERIC(8,4),
    status              VARCHAR(15) DEFAULT 'proposed',
    proposed_at         TIMESTAMPTZ DEFAULT NOW(),
    filled_at           TIMESTAMPTZ,
    closed_at           TIMESTAMPTZ,
    final_pnl           NUMERIC(12,2),
    notes               TEXT
);
CREATE INDEX IF NOT EXISTS idx_fno_signals_status ON fno_signals(status);
CREATE INDEX IF NOT EXISTS idx_fno_signals_underlying
    ON fno_signals(underlying_id, proposed_at DESC);

CREATE TABLE IF NOT EXISTS fno_signal_events (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    signal_id       UUID NOT NULL REFERENCES fno_signals(id),
    from_status     VARCHAR(15),
    to_status       VARCHAR(15) NOT NULL,
    reason          TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_fno_signal_events_signal
    ON fno_signal_events(signal_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ranker_configs (
    version           VARCHAR(20) PRIMARY KEY,
    weights           JSONB NOT NULL,
    activated_at      TIMESTAMPTZ DEFAULT NOW(),
    deactivated_at    TIMESTAMPTZ,
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS fno_cooldowns (
    underlying_id     UUID NOT NULL REFERENCES instruments(id),
    cooldown_until    TIMESTAMPTZ NOT NULL,
    reason            VARCHAR(50),
    PRIMARY KEY (underlying_id, cooldown_until)
);
"""

_DOWNGRADE_SQL = """
DROP TABLE IF EXISTS fno_cooldowns CASCADE;
DROP TABLE IF EXISTS ranker_configs CASCADE;
DROP TABLE IF EXISTS fno_signal_events CASCADE;
DROP TABLE IF EXISTS fno_signals CASCADE;
DROP TABLE IF EXISTS fno_candidates CASCADE;
DROP TABLE IF EXISTS vix_ticks CASCADE;
DROP TABLE IF EXISTS iv_history CASCADE;
DROP TABLE IF EXISTS options_chain CASCADE;
DROP TABLE IF EXISTS fno_ban_list CASCADE;
"""


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
