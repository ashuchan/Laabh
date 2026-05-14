-- ============================================================================
-- Migration 2026-05-15: LLM-as-feature-generator data layer (Phase 0.1)
--
-- Adds two tables that back the LLM Feature Generator initiative:
--   1. llm_decision_log    — one row per (run_date, instrument_id, llm_call)
--                            storing raw + parsed LLM output, the bandit
--                            propensity at decision time, and the eventual
--                            attributed outcome.
--   2. llm_calibration_models — versioned calibration curves (Platt / isotonic)
--                            keyed by (prompt_version, phase, feature_name,
--                            instrument_tier, fitted_at).
--
-- Schema rationale lives in docs/llm_feature_generator/implementation_plan.md
-- §0.1. instrument_id is UUID to match instruments(id) and
-- fno_candidates.instrument_id.
--
-- Idempotent: every CREATE uses IF NOT EXISTS so re-runs are no-ops.
-- ============================================================================

CREATE TABLE IF NOT EXISTS llm_decision_log (
    id                          BIGSERIAL PRIMARY KEY,
    run_date                    DATE NOT NULL,
    as_of                       TIMESTAMPTZ NOT NULL,
    dryrun_run_id               UUID NULL,
    instrument_id               UUID NOT NULL REFERENCES instruments(id),
    phase                       TEXT NOT NULL,
        -- 'fno_thesis' | 'quant_universe'
    prompt_version              TEXT NOT NULL,
        -- 'v9' (legacy categorical) | 'v10_continuous'
    model_id                    TEXT NOT NULL,
        -- pinned snapshot e.g. 'claude-sonnet-4-20250514'

    -- Legacy categorical (kept for backwards compat in shadow phase)
    decision_label              TEXT NULL,
        -- PROCEED | SKIP | HEDGE | NULL

    -- Continuous features (Phase 1 populates; NULL for legacy v9-only rows)
    directional_conviction      REAL NULL,    -- [-1, +1]
    thesis_durability           REAL NULL,    -- [0, 1]
    catalyst_specificity        REAL NULL,    -- [0, 1]
    risk_flag                   REAL NULL,    -- [-1, 0]
    raw_confidence              REAL NULL,    -- model self-stated probability [0, 1]

    -- Calibrated values (Phase 2 populates)
    calibrated_conviction       REAL NULL,
    calibration_model_id        INT NULL,     -- FK populated below

    -- Outcome — filled on position close (NOT end-of-day; see plan §0.3)
    outcome_pnl_pct             REAL NULL,
    outcome_z                   REAL NULL,    -- pnl_pct / expected_vol_at_entry
    outcome_class               TEXT NULL,    -- 'traded' | 'counterfactual' | 'unobservable' | 'timeout'

    -- Bandit context at the moment of the LLM call (IPS-weight inputs).
    -- Stashed here rather than in a separate signal_log because no live
    -- per-decision bandit-log table exists today.
    bandit_posterior_mean       REAL NULL,
    bandit_posterior_var        REAL NULL,
    bandit_arm_propensity       REAL NULL,    -- derived: P(this arm chosen | context)

    outcome_attributed_at       TIMESTAMPTZ NULL,

    -- Full LLM payload for audit / re-parse
    raw_response                JSONB NOT NULL,

    UNIQUE (run_date, instrument_id, phase, prompt_version, dryrun_run_id)
);

-- Outcome attribution job filters on this — find rows still waiting for
-- their position to close.
CREATE INDEX IF NOT EXISTS idx_llm_log_outcome_pending
    ON llm_decision_log(outcome_attributed_at)
    WHERE outcome_attributed_at IS NULL;

-- Calibration fitter filters on this — rows ready to train on.
CREATE INDEX IF NOT EXISTS idx_llm_log_calibration_ready
    ON llm_decision_log(prompt_version, phase, outcome_attributed_at)
    WHERE outcome_z IS NOT NULL AND outcome_class IN ('traded', 'counterfactual');

-- Lineage join helper (run_date + instrument_id + phase) used by the
-- outcome attribution path to locate the LLM row from a closed fno_signal.
CREATE INDEX IF NOT EXISTS idx_llm_log_lineage
    ON llm_decision_log(run_date, instrument_id, phase);

-- Convert to TimescaleDB hypertable when available; otherwise plain table
-- (matches the established pattern at e.g. vix_ticks).
DO $$
BEGIN
    PERFORM create_hypertable('llm_decision_log', 'as_of',
        chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE);
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'TimescaleDB unavailable -- llm_decision_log is a plain table';
END
$$;

-- ============================================================================
-- Versioned calibration curves (one row = one fitted model).
-- ============================================================================
CREATE TABLE IF NOT EXISTS llm_calibration_models (
    id                  SERIAL PRIMARY KEY,
    fitted_at           TIMESTAMPTZ NOT NULL,
    prompt_version      TEXT NOT NULL,
    phase               TEXT NOT NULL,
    feature_name        TEXT NOT NULL,
        -- 'directional_conviction' | 'raw_confidence'
    instrument_tier     TEXT NOT NULL,
        -- 'T1' | 'T2' | 'pooled'
    method              TEXT NOT NULL,
        -- 'platt' (small N) | 'isotonic' (large N)
    n_observations      INT NOT NULL,
    params              JSONB NOT NULL,
        -- Platt:   {a, b}
        -- Isotonic: {x_knots, y_knots}
    cv_ece              REAL NULL,    -- expected calibration error (walk-forward)
    cv_residual_var     REAL NULL,    -- Var(outcome_z | calibrated)
    is_active           BOOLEAN DEFAULT FALSE,
    UNIQUE (prompt_version, phase, feature_name, instrument_tier, fitted_at)
);

-- Phase 3 reads the latest active model per (prompt_version, phase,
-- feature_name, instrument_tier) on every bandit decision -- index it.
CREATE INDEX IF NOT EXISTS idx_llm_calib_active
    ON llm_calibration_models(prompt_version, phase, feature_name, instrument_tier)
    WHERE is_active;

-- Wire the FK from llm_decision_log -> llm_calibration_models now that both
-- tables exist. Done as ALTER so the create order above stays readable.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'llm_decision_log_calib_fk'
    ) THEN
        ALTER TABLE llm_decision_log
            ADD CONSTRAINT llm_decision_log_calib_fk
            FOREIGN KEY (calibration_model_id)
            REFERENCES llm_calibration_models(id)
            ON DELETE SET NULL;
    END IF;
END
$$;

-- ============================================================================
-- Phase 0.5 baseline: deterministic six-factor top-K per run_date.
-- Tracked here so the LLM-vs-deterministic Sharpe comparison has a
-- per-day record to query (plan §0.5.2).
-- ============================================================================
CREATE TABLE IF NOT EXISTS quant_universe_baseline (
    id                  BIGSERIAL PRIMARY KEY,
    run_date            DATE NOT NULL,
    instrument_id       UUID NOT NULL REFERENCES instruments(id),
    rank                INT NOT NULL,
    composite_score     REAL NOT NULL,
    -- Sub-scores (z-scored within run_date over the 60-day rolling window).
    z_liquidity         REAL NULL,
    z_iv_rank_momentum  REAL NULL,
    z_rv_regime         REAL NULL,
    z_trend_strength    REAL NULL,
    z_mean_reversion    REAL NULL,
    z_microstructure    REAL NULL,
    composite_version   TEXT NOT NULL DEFAULT 'v0_equal',
        -- 'v0_equal' = equal-weight, 'v1_pca' = PCA loadings re-fit monthly
    dryrun_run_id       UUID NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_date, instrument_id, dryrun_run_id)
);

CREATE INDEX IF NOT EXISTS idx_quant_universe_baseline_run
    ON quant_universe_baseline(run_date, rank);

-- ============================================================================
-- Bandit update-context propagation (review fix P0 #1, P2 #5).
--
-- The LinTS update path needs the same context vector the selector saw at
-- entry, otherwise the bandit "selects with LLM" but "learns with zero
-- context" — coefficients on the LLM dims never converge. Persisting on
-- BOTH quant_trades (live) and backtest_trades (replay) ensures the same
-- correctness holds for backtest reconstructions.
-- ============================================================================
ALTER TABLE quant_trades
    ADD COLUMN IF NOT EXISTS entry_context JSONB NULL;

ALTER TABLE backtest_trades
    ADD COLUMN IF NOT EXISTS entry_context JSONB NULL;

-- ============================================================================
-- Realised-vol snapshot at entry (review fix P1 #5).
--
-- Phase 0.3 outcome attribution wants ``feature_store.rv_30min`` for the
-- ``outcome_z`` denominator (plan §0.3). The feature_store value is only
-- accurate at the entry moment; recompute-at-close would give the wrong
-- σ. Snapshot the best available annualised RV at entry so the
-- attribution job reads a stable value.
--
-- Column name is intentionally generic — at entry, the writer prefers
-- ``feature_store.rv_30min`` and falls back to ``iv_history.rv_20d`` when
-- intraday bars aren't yet built.
-- ============================================================================
ALTER TABLE fno_signals
    ADD COLUMN IF NOT EXISTS rv_annualised_at_entry NUMERIC(8, 4) NULL;
