-- Migration: 2026-05-10
-- Adds backtest_signal_log — per-tick record of every signal a primitive
-- emitted during a backtest replay, plus the reason it did or didn't trade.
-- Powers the "missed trades" / selection-funnel report.
-- Idempotent: re-running is a no-op.

CREATE TABLE IF NOT EXISTS backtest_signal_log (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    backtest_run_id     UUID NOT NULL REFERENCES backtest_runs(id),
    virtual_time        TIMESTAMPTZ NOT NULL,
    underlying_id       UUID NOT NULL REFERENCES instruments(id),
    symbol              VARCHAR(50) NOT NULL,
    arm_id              VARCHAR(80) NOT NULL,
    primitive_name      VARCHAR(30) NOT NULL,
    direction           VARCHAR(20) NOT NULL,
    strength            NUMERIC(6,4) NOT NULL,
    -- One of: opened, weak_signal, warmup, kill_switch, capacity_full,
    -- cooloff, lost_bandit, sized_zero
    rejection_reason    VARCHAR(20) NOT NULL,
    posterior_mean      NUMERIC(10,6),
    bandit_selected     BOOLEAN NOT NULL DEFAULT FALSE,
    lots_sized          INT
);

CREATE INDEX IF NOT EXISTS idx_backtest_signal_log_run
    ON backtest_signal_log(backtest_run_id);
CREATE INDEX IF NOT EXISTS idx_backtest_signal_log_run_symbol
    ON backtest_signal_log(backtest_run_id, symbol);
CREATE INDEX IF NOT EXISTS idx_backtest_signal_log_run_reason
    ON backtest_signal_log(backtest_run_id, rejection_reason);
