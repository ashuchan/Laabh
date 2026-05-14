-- ============================================================================
-- Migration 2026-05-13: VRP Engine columns on iv_history
--
-- Adds realized volatility and Volatility Risk Premium (VRP) columns so the
-- EOD iv_history_builder + vrp_engine pipeline can persist daily VRP readings.
--
-- VRP = ATM_IV (annualized, decimal) - RV_20d (Yang-Zhang realized vol)
-- Positive VRP → IV is overpriced → premium-selling opportunity
-- Negative VRP → IV is cheap → avoid selling, watch for tail risk
--
-- Idempotent: safe to re-run (ADD COLUMN IF NOT EXISTS).
-- ============================================================================

ALTER TABLE iv_history
    ADD COLUMN IF NOT EXISTS rv_20d     NUMERIC(8, 4),   -- 20-day realized vol (annualized, decimal)
    ADD COLUMN IF NOT EXISTS vrp        NUMERIC(8, 4),   -- VRP = atm_iv_decimal - rv_20d
    ADD COLUMN IF NOT EXISTS vrp_regime VARCHAR(10)      -- 'rich' | 'fair' | 'cheap'
        CHECK (vrp_regime IN ('rich', 'fair', 'cheap'));

-- Fast lookup for Phase 3 (reads latest VRP per instrument before market open)
CREATE INDEX IF NOT EXISTS idx_iv_history_inst_date_desc
    ON iv_history(instrument_id, date DESC);
