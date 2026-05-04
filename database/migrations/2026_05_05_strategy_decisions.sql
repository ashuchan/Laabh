-- Migration: 2026-05-05
-- Adds strategy_decisions for the LLM-driven equity paper-trading layer.
-- Idempotent: re-running is a no-op.

CREATE TABLE IF NOT EXISTS strategy_decisions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    portfolio_id        UUID NOT NULL REFERENCES portfolios(id),
    decision_type       VARCHAR(40) NOT NULL,
    as_of               TIMESTAMPTZ NOT NULL,
    risk_profile        VARCHAR(20),
    budget_available    NUMERIC(15,2),
    input_summary       JSONB,
    llm_model           VARCHAR(80),
    llm_reasoning       TEXT,
    actions_json        JSONB NOT NULL,
    actions_executed    INT DEFAULT 0,
    actions_skipped     INT DEFAULT 0,
    dryrun_run_id       UUID,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_strategy_decisions_portfolio
    ON strategy_decisions(portfolio_id, as_of DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_decisions_type
    ON strategy_decisions(decision_type, as_of DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_decisions_dryrun
    ON strategy_decisions(dryrun_run_id) WHERE dryrun_run_id IS NOT NULL;
