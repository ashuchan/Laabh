-- Migration: 2026-05-10 (part 2)
-- Adds three JSONB trace columns to backtest_signal_log so the Decision
-- Inspector can render formula cards, sizer cascades, and bandit tournaments.
--
-- Population semantics (by orchestrator + recorder):
--   primitive_trace : ALWAYS set when a row exists. Captures the primitive's
--                     inputs, intermediates, and a human-readable formula
--                     with values plugged in.
--   bandit_trace    : Set when this arm participated in bandit selection
--                     (buckets: lost_bandit, sized_zero, opened). NULL for
--                     weak_signal / cooloff / kill_switch / capacity_full /
--                     warmup since those rows never reached the bandit.
--   sizer_trace     : Set ONLY on the chosen arm's row. Carries the full
--                     9-step Kelly cascade.
--
-- Idempotent: re-running is a no-op.

ALTER TABLE backtest_signal_log
    ADD COLUMN IF NOT EXISTS primitive_trace JSONB,
    ADD COLUMN IF NOT EXISTS bandit_trace    JSONB,
    ADD COLUMN IF NOT EXISTS sizer_trace     JSONB;
