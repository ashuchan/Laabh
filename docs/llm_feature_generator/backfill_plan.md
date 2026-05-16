# Composite Backfill + Bootstrap Calibration Plan — v2

*Supersedes [backfill_plan.md](backfill_plan.md). Covers the two-track strategy to begin LLM-feature calibration on Monday 2026-05-18 with both a forward live-data stream and a 6-month historical backfill scaffolding the bootstrap calibration model. Incorporates Dhan Data API intraday availability (procured 2026-05-15), tier hydration, point-in-time news policy, and holdout reservation.*

**Owner**: ashuchan
**Drafted**: 2026-05-15 (Friday afternoon IST)
**Target first bootstrap fit (Stage 1, daily-resolution)**: Sunday 2026-05-17 evening
**Target Stage 2 bootstrap fit (intraday-resolution)**: Sunday 2026-05-24 evening
**Target live v10 logging start**: Monday 2026-05-18, 07:00 IST (Phase 3 cron)

---

## 0. What changed since v1

| Topic | v1 | v2 |
|---|---|---|
| Dhan intraday subscription | Pending decision; recommended yfinance fallback | **Procured.** Used for Stage 2 intraday-resolution outcome attribution. |
| Outcome attribution resolution | Daily-close bhavcopy proxy only | Stage 1: daily proxy; Stage 2: intraday entry → intraday exit using `price_intraday`. |
| Tier hydration | Not addressed (T1/T2 fits silently empty) | Explicit step in Phase A; populated on every backfill row + on live rows by retrofit job. |
| News context | "Best-effort, not reconstructed" | Explicit `published_at ≤ ist(D, 9, 0)` filter; backfill is allowed to include news only if it was already published by that historical morning. |
| Phase 0.5 deterministic baseline | Out of scope for backfill | Backfilled in parallel during Phase A so head-to-head Sharpe comparison is available on the same dates. |
| Bandit propensity for backfill rows | `COALESCE(propensity, 1/n_arms)` band-aid | Distinct `propensity_source` column + 0.3× weight multiplier for imputed propensities (same hygiene as counterfactual rows). |
| Resume granularity | `(date, batch_uuid)` keyed | `(date, candidate_id, batch_uuid)` keyed so partial-day failures resume per-candidate. |
| Cost discipline | `sleep(2 / max_concurrent)` between dates | Token-bucket rate limiter against Anthropic tier limits; prompt caching enabled (per project pattern). |
| Holdout | Walk-forward CV only | Reserve D-14..D-1 as held-out; fit Stage 1 on D-180..D-15; report holdout ECE alongside CV ECE. |
| Prompt-injection defense | Implicit | Apply the same defense the live path uses (`src/fno/news_sanitizer.py` or equivalent) to historical RSS items before they enter the prompt. |

---

## 1. Goal

Produce calibrated v10 feature → `outcome_z` models that the bandit can consume under `LAABH_LLM_MODE=feature`, using the minimum-side-effect path:

1. **Live forward track** — every Phase-3 candidate from Monday onward emits a v10 row to `llm_decision_log` regardless of `LAABH_LLM_MODE`. v9 still gates production decisions. ~9 rows/day, ~225 rows by Day 25.
2. **Historical backfill track — Stage 1 (daily-resolution)** — replay v10 LLM calls against the previous 180 trading days with `dryrun_run_id` scoping. Yields ~900-1800 rows over the weekend, with counterfactual outcomes auto-attributed from end-of-day bhavcopy.
3. **Historical backfill track — Stage 2 (intraday-resolution)** — re-attribute the Stage 1 LLM rows using Dhan intraday (`price_intraday`) entry/exit times instead of bhavcopy close. Refits bootstrap calibration with higher-fidelity `outcome_z`.
4. **Bootstrap calibration** — fit Platt/isotonic models scoped to the backfill UUID. Stage 1 is the null hypothesis the live calibration must beat over the next 4-6 weeks before `LAABH_LLM_MODE` flips to `feature`. Stage 2 is the refinement we report against once intraday data is loaded.

Crucially: **we do not trade on the bootstrap models on Monday.** Bootstrap is a benchmark and accelerant for validating live calibration, not a substitute for the plan's mandated 10-day shadow safety period.

---

## 2. Architecture: two tracks, two stages

```
                    ┌─────────────────────────────┐
Mon 2026-05-18 →    │ Live forward track          │  → llm_decision_log (live scope)
07:00 IST onward    │ (Phase 3 cron, v9 + v10)    │     ~9 rows/day
                    └─────────────────────────────┘
                                                          ↓
                                                    weekly calibration
                                                    (Sun 22:00 IST)

                    ┌─────────────────────────────┐
Sat 2026-05-16 →    │ Backfill Stage 1 (daily)    │  → llm_decision_log (dryrun scope)
Sun 2026-05-17      │ - prereqs (Phase A)         │     ~900-1800 rows
                    │ - LLM replay (Phase B)      │  → bootstrap_v1 fit
                    │ - EOD bhavcopy outcomes (C) │
                    │ - Bootstrap fit (D)         │
                    └─────────────────────────────┘
                                  ↓
                    background:
Fri 2026-05-15 →    ┌─────────────────────────────┐
Sun 2026-05-24      │ Dhan intraday backfill      │  → price_intraday
(rate-limited)      │ (28-42 hr wall clock)       │     ~4-6M rows
                    └─────────────────────────────┘
                                                          ↓
                    ┌─────────────────────────────┐
Sun 2026-05-24      │ Backfill Stage 2 (intraday) │  → llm_decision_log
                    │ - re-attribute outcome_z    │     (existing rows updated)
                    │ - refit bootstrap_v2        │  → bootstrap_v2 fit
                    └─────────────────────────────┘
```

---

## 3. The two tracks

### 3.1 Forward (live) track — unchanged from v1

`LAABH_LLM_V10_LOGGING_ENABLED=true` is the default after the option-3 wiring change. From Monday 07:00 IST onward:

- Every Phase-3 v9 call also fires a parallel v10 LLM call via `asyncio.create_task(_log_v10_shadow(...))`
- v10 result written to `llm_decision_log` with `prompt_version='v10_continuous'`, `dryrun_run_id=NULL` (live scope)
- Outcome attribution job fills `outcome_pnl_pct / outcome_z / outcome_class` on position close (traded) or T+5 (counterfactual) or T+30 (timeout)
- Sunday 22:00 IST weekly calibration job auto-fits when N≥100 in live scope

**Cost / cadence projection**:
- ~9 candidates/day × 1 v10 call × $0.005-0.03 = **₹3-25/day** (Haiku to Sonnet)
- ~45 rows/week → ~180 rows/4-week period → live Platt fittable at T+12, live isotonic at T+25 trading days
- First *trustworthy* live fit ETA: ~Friday 2026-06-05 (Platt, N≈110) to Friday 2026-06-19 (isotonic, N≈225)

### 3.2 Historical backfill track — Stage 1 (this weekend)

Three idempotent scripts, all dashboard-launchable as subprocesses:

```
scripts/ensure_historical_prereqs.py    (deterministic, no LLM)
scripts/backfill_llm_features.py         (LLM-driven, rate-limited, resumable per candidate)
scripts/promote_bootstrap_calibration.py (wraps existing weekly calibration; reports holdout ECE)
```

### 3.3 Historical backfill track — Stage 2 (next weekend, after Dhan intraday lands)

Two additional scripts:

```
scripts/dhan_intraday_backfill.py        (Dhan API, 28-42 hr wall clock, fully resumable)
scripts/reattribute_outcome_z_intraday.py (re-runs outcome attribution using price_intraday)
```

Stage 2 does **not** re-run the LLM. It re-uses the v10 features already in `llm_decision_log` from Stage 1 and only updates `outcome_pnl_pct / outcome_z / outcome_class` columns. This keeps the LLM spend a one-time cost.

---

## 4. Phase-by-phase execution — Stage 1

### Phase A — Historical prerequisites backfill (Saturday)

**Goal**: For each historical trading day in D-180..D-1, ensure all inputs the v10 prompt, outcome attribution, and per-tier calibration need are present in the DB.

**Inputs**: Bhavcopy archive (NSE F&O + CM), price_daily, RSS archive filtered by `published_at`.

**Outputs (per historical date D)**:
- `price_daily` row — already covers 2026-02-04 to 2026-05-14 per probe; extend via yfinance to 2025-11-18
- `iv_history.rv_20d / vrp / vrp_regime` populated (currently sparse: 15 days of 1966 total)
- `vol_surface_snapshot` row — computed from F&O bhavcopy
- `market_regime_snapshot` row — `compute_regime(d, as_of=ist(d,9,0))`
- `fno_candidates` Phase 1 + Phase 2 rows — `run_phase1(d, as_of=...)`, `run_phase2(d)`. No LLM.
- **NEW** `fno_candidates.instrument_tier` populated — derived from existing tiering logic at `src/fno/tier_manager.py` (T1 = top-35 polling tier, T2 = the other ~170, indices flagged separately).
- **NEW** `deterministic_universe_snapshot` row — Phase 0.5 six-factor composite top-K=25 for date D.

**Script contract**: `scripts/ensure_historical_prereqs.py`
```
usage: ensure_historical_prereqs.py --days N [--from DATE] [--to DATE]
                                    [--skip-existing] [--concurrency K]

Iterates trading days backwards from --to (default yesterday) for --days days
(default 180). For each date, with date-level concurrency = K (default 4):
  1. Skip if --skip-existing and all sentinel rows exist.
  2. Fetch bhavcopy if not cached.
  3. Backfill price_daily via yfinance if missing.
  4. Compute iv_history.rv_20d/vrp/vrp_regime.
  5. Compute vol_surface_snapshot.
  6. Compute market_regime_snapshot (as_of=ist(D, 9, 0)).
  7. Replay Phase 1+2 (as_of=ist(D, 9, 0)). Populate instrument_tier on each row.
  8. Compute deterministic_universe_snapshot (six-factor z-score composite, top-25).

Idempotent: each step is a no-op if its output row already exists.

Reports: per-day progress, final summary of dates filled / dates failed.
```

**Acceptance test (Saturday EOD)**:
```sql
-- Coverage check
SELECT COUNT(DISTINCT date) FROM iv_history WHERE rv_20d IS NOT NULL;        -- ≥180
SELECT COUNT(DISTINCT date) FROM vol_surface_snapshot;                        -- ≥180
SELECT COUNT(DISTINCT run_date) FROM market_regime_snapshot;                  -- ≥180
SELECT COUNT(DISTINCT run_date) FROM fno_candidates WHERE phase=2;            -- ≥180

-- Tier hydration check (NEW)
SELECT instrument_tier, COUNT(*)
FROM fno_candidates
WHERE phase=2 AND run_date >= CURRENT_DATE - 180
GROUP BY instrument_tier;
-- Expect: T1 ≈ 30-50%, T2 ≈ 50-70%, indices ≈ small, NULL = 0.

-- Deterministic baseline coverage (NEW)
SELECT COUNT(DISTINCT run_date) FROM deterministic_universe_snapshot;         -- ≥180
```

**Estimated wall time**: 1.5-3 hours with concurrency=4.

### Phase B — LLM feature replay (Sunday morning)

**Goal**: For each historical date with prerequisites in place, replay Phase 3 with the v10 prompt and write v10 features to `llm_decision_log` under one `dryrun_run_id`.

**Strategy**: stage rollout to validate look-ahead discipline before scaling spend.

**News context policy (NEW, critical)**:
- Phase 3 prompts include recent news context. For a replayed historical date D, only RSS items with `published_at ≤ ist(D, 9, 0)` are eligible.
- Each prompt-bound news item is run through the same prompt-injection sanitizer the live path uses, before being templated into the prompt.
- If no eligible news exists for an underlying, the prompt section is empty (matching how live behaves on quiet news days).

**Prompt caching (NEW)**: enable Anthropic prompt caching on the static portion of the v10 prompt template. Cuts per-call cost ~30-50% on Sonnet.

**Script contract**: `scripts/backfill_llm_features.py`
```
usage: backfill_llm_features.py --days N --batch-id LABEL
                                [--from DATE] [--to DATE]
                                [--max-concurrent K]
                                [--max-cost USD] [--dry-run]
                                [--holdout-tail-days N]

Generates a stable UUID by hashing --batch-id ("BACKFILL_v1" → fixed UUID).

Reserves the most recent --holdout-tail-days trading days (default 15) and
records them in a holdout sentinel table. These dates are STILL backfilled
(so we can score the bootstrap models against them) but Phase D promotion
fits use only D-180..D-(holdout+1) for the actual fit data.

Token-bucket rate limiter against Anthropic tier limits (default: 45 req/min,
~80% of tier 1's 50 req/min ceiling). Configurable per env var.

For each historical date D in --days window:
  - if no Phase 2 candidates exist for D, skip with warning
  - for each candidate_id in D's Phase 2 set:
      - if v10 row already exists for (D, candidate_id, batch_uuid), skip (resume)
      - else:
          with set_dryrun_run_id(batch_uuid):
              v10_result = await run_phase3_single(
                  D, candidate_id, as_of=ist(D, 9, 0),
                  dryrun_run_id=batch_uuid,
                  news_cutoff=ist(D, 9, 0),  # point-in-time filter
                  enable_prompt_cache=True,
              )

Stops early if --max-cost reached (estimated from tokens used so far).
Partial-day failure → safe to re-run; resumes per candidate.

Reports: per-day rows written, total cost so far, ETA.
```

**Two-pass rollout**:
- **Pass 1 (Sunday 10:00 IST)**: `--days 30 --batch-id BACKFILL_v1 --max-cost 15`
  - ~270 v10 rows, ~$3-9 spend
  - Validates look-ahead discipline + prompt context completeness + news policy
- **Pass 2 (Sunday 13:00 IST, only if Pass 1 ECE looks reasonable)**: `--days 180 --batch-id BACKFILL_v1`
  - ~1620 additional v10 rows, ~$10-40 incremental spend

**Acceptance test (Sunday by 14:00 IST)**:
```sql
-- Coverage check after Pass 2
SELECT COUNT(*), COUNT(DISTINCT run_date)
FROM llm_decision_log
WHERE dryrun_run_id = '<BACKFILL_v1_UUID>'
  AND prompt_version = 'v10_continuous';
-- Expect: COUNT(*) ≥ 1500, run_dates ≥ 150

-- Degenerate-output rate (LLM lazy-default flag)
SELECT
  100.0 * SUM(CASE WHEN directional_conviction = 0 AND thesis_durability = 0
                    AND catalyst_specificity = 0 AND risk_flag = 0
                    THEN 1 ELSE 0 END) / COUNT(*) AS pct_degenerate
FROM llm_decision_log
WHERE dryrun_run_id = '<BACKFILL_v1_UUID>';
-- Expect: < 5%

-- Tier coverage check (NEW)
SELECT instrument_tier, COUNT(*)
FROM llm_decision_log
WHERE dryrun_run_id = '<BACKFILL_v1_UUID>'
GROUP BY instrument_tier;
-- Expect: all rows have non-NULL tier; T1 ≥ 300, T2 ≥ 800.

-- News cutoff sanity (NEW) — no prompt should reference a published_at later than its as_of
SELECT COUNT(*)
FROM llm_decision_log l, jsonb_array_elements(l.raw_response->'news_items_used') n
WHERE l.dryrun_run_id = '<BACKFILL_v1_UUID>'
  AND (n->>'published_at')::timestamptz > l.as_of;
-- Expect: 0
```

### Phase C — Outcome attribution (Stage 1, daily resolution)

**Existing 5-min `fno_llm_outcomes` job** ([scheduler.py:867](../../src/scheduler.py#L867), [llm_outcomes.py](../../src/fno/llm_outcomes.py)) polls `llm_decision_log` for rows where `outcome_attributed_at IS NULL` and runs counterfactual P&L from bhavcopy.

By default this filters `dryrun_run_id IS NULL` (live scope only). For backfill we extend the existing entry point with a `--dryrun-run-id` CLI flag for one-shot attribution.

Add to `scripts/backfill_llm_features.py` as a final step:
```python
await attribute_llm_outcomes(
    dryrun_run_id=batch_uuid,
    resolution="daily",          # Stage 1
)
```

**Bandit propensity for backfill rows (NEW)**:
- Backfilled rows have no live bandit decision behind them. Naive `COALESCE(propensity, 1/n_arms)` biases IPS weights.
- Schema addition: `llm_decision_log.propensity_source VARCHAR(20)` with values `'live'` (real bandit decision) | `'imputed'` (1/n_arms heuristic) | `'unknown'`.
- Backfill rows set `propensity_source = 'imputed'`, propensity = `1/n_arms_today`.
- Calibration code applies an additional 0.3× weight multiplier when `propensity_source = 'imputed'` (same hygiene as counterfactual rows already get).

**Acceptance test**:
```sql
SELECT outcome_class, COUNT(*)
FROM llm_decision_log
WHERE dryrun_run_id = '<BACKFILL_v1_UUID>'
  AND outcome_attributed_at IS NOT NULL
GROUP BY outcome_class;
-- Expect: 'counterfactual' dominates (since no real trades); some 'unobservable'
-- (thin or missing chain); zero 'traded' (no live positions for replayed dates).
-- Total attributed ≥ 60% of backfill rows.

-- Propensity source check (NEW)
SELECT propensity_source, COUNT(*)
FROM llm_decision_log
WHERE dryrun_run_id = '<BACKFILL_v1_UUID>'
GROUP BY propensity_source;
-- Expect: 100% 'imputed' for the backfill UUID.
```

### Phase D — Bootstrap calibration fit (Stage 1)

**Goal**: Produce one fitted calibration model per `(feature, instrument_tier)` under the backfill scope, evaluated on a true holdout.

**Script contract**: `scripts/promote_bootstrap_calibration.py`
```
usage: promote_bootstrap_calibration.py --batch-id LABEL [--promote]
                                        [--report-holdout]

1. Resolve batch_id → batch_uuid.
2. Determine the holdout window: most recent --holdout-tail-days from backfill.
3. Call run_weekly_calibration(
        dryrun_run_id=batch_uuid,
        exclude_dates_after=holdout_start,   # NEW — true holdout
        as_of=holdout_start,
    )
   This fits per (feature ∈ {directional_conviction, raw_confidence},
                  phase='fno_thesis',
                  instrument_tier ∈ {'T1', 'T2', 'pooled'}).
   Method = Platt when N < 500, isotonic otherwise.
4. Score each fit on the holdout window (D-14..D-1):
     - holdout_ece = ECE on holdout
     - holdout_residual_var = Var(outcome_z - predicted) on holdout
5. Print fit metrics per (feature, tier): N, method, cv_ece, holdout_ece,
   cv_residual_var, holdout_residual_var.
6. Render reliability diagram PNGs (CV + holdout) to
   apps/static/calibration/<fitted_at>_bootstrap_<feature>_<tier>.png.
7. If --promote AND holdout_ece < 0.10 AND holdout_residual_var < 1.5
   for at least 2 (feature, tier) pairs:
     Copy fitted params into a new row in llm_calibration_models with
     dryrun_run_id=NULL and is_active=true (deactivating any existing
     active row for the same key).
   Else: print which (feature, tier) failed the threshold and skip promote.
```

**Promotion threshold tightened (NEW)**: use `holdout_ece` (true out-of-sample) rather than `cv_ece` (within-sample walk-forward). Holdout is the harder bar.

**Acceptance test**:
```sql
-- Without --promote
SELECT feature_name, instrument_tier, method, n_observations,
       cv_ece, holdout_ece, cv_residual_var, holdout_residual_var,
       is_active
FROM llm_calibration_models
WHERE prompt_version = 'v10_continuous'
ORDER BY fitted_at DESC;
-- Expect: 4-6 rows (2 features × 2-3 tiers), all is_active=false,
-- both ECE columns populated, holdout_ece typically 0.02-0.04 above cv_ece.
```

### Phase E — Validation & Monday-morning decision (Stage 1)

**Sunday 16:00-22:00 IST**: human review of fitted models.

**Decision tree**:
- All fits have `holdout_ece < 0.08`: confident bootstrap; keep as reference, **do not promote** to live scope.
- `holdout_ece` 0.08-0.15: marginal bootstrap; useful as benchmark but unreliable for trading.
- `holdout_ece > 0.15` or holdout residual variance huge: look-ahead discipline likely broken — investigate (see §7).
- **Sanity floor**: if `|cv_ece - holdout_ece| > 0.05`, walk-forward CV is over-optimistic → suspect leakage in the CV split or in the news-context cutoff; do not promote, investigate.

**No mode flip on Monday regardless of bootstrap quality.** `LAABH_LLM_MODE` stays at `gate` through the canonical 10-trading-day shadow period. Bootstrap is a research artifact, not a production model.

---

## 5. Phase-by-phase execution — Stage 2 (next weekend)

### Phase F — Dhan intraday backfill (background, starts Friday evening)

**Goal**: Populate `price_intraday` with 3-min bars for the F&O universe over the same 180-day window as Stage 1.

**Wall-clock arithmetic** (from `quant_backtest_runbook.md`):
- 30 req/min × 60 min/hr = 1,800 req/hr
- Full F&O universe ≈ 200 instruments
- 180 trading days × 1 call per (instrument, 30-day chunk) ≈ 200 × 6 = 1,200 calls per instrument-window
- Total ≈ 36,000-50,000 calls ≈ **20-28 hours of wall clock** at the default budget

**Script reuse**: this is exactly Task 2 of `CLAUDE-FNO-TASK-QUANT-BACKTEST.md` — `src/quant/backtest/data_loaders/dhan_historical.py`. **No new script needed.** Use the same runbook command:
```bash
python -m src.quant.backtest.data_loaders.dhan_historical \
  --start 2025-11-18 --end 2026-05-14 --only-fno
```

**Operational note**: launch Friday evening, monitor via logs Saturday, expect completion late Sunday or early Monday. Stage 2 attribution does not block on completion — it can run on whatever subset of dates has intraday data.

### Phase G — Re-attribute outcome_z with intraday resolution

**Goal**: For each Stage 1 row in `llm_decision_log` (dryrun_run_id = BACKFILL_v1), recompute `outcome_pnl_pct / outcome_z` using `price_intraday` instead of daily bhavcopy close.

**Method** (mirrors live attribution):
- Entry time: `ist(D, 9, 30)` (or proposed_entry_time if v10 specified one)
- Entry price: `price_intraday.close` at entry tick for the proposed strike (synthesized via BS from Tier 1 intraday + Tier 2 daily IV, same approach as backtest harness chain synthesizer)
- Exit time: `min(proposed_horizon, ist(D, 15, 15))` — capped at 15 min before close, matching live exits
- Exit price: `price_intraday.close` at exit tick
- `outcome_pnl_pct = (exit - entry) × direction_sign / entry`
- `outcome_z = outcome_pnl_pct / expected_vol_at_entry`

**Script contract**: `scripts/reattribute_outcome_z_intraday.py`
```
usage: reattribute_outcome_z_intraday.py --batch-id LABEL [--dry-run]

For each row in llm_decision_log with dryrun_run_id matching batch_id:
  - Skip if no price_intraday coverage for that (instrument, date).
  - Recompute outcome_pnl_pct / outcome_z using intraday entry/exit.
  - Update outcome_class:
      'counterfactual_intraday' if attributed via intraday
      'counterfactual_eod'      (preserved) if intraday data missing
  - Bump outcome_attributed_at to now.

Reports: rows updated, rows kept at eod fallback, distribution shift in outcome_z.
```

**Outcome class taxonomy update**: `outcome_class` gains `counterfactual_intraday` alongside the existing `counterfactual` (rename existing → `counterfactual_eod` for clarity). Calibration code treats both as counterfactual (0.3× weight); only the audit trail differs.

### Phase H — Bootstrap v2 fit

Re-run `promote_bootstrap_calibration.py` with the same UUID. Expected outcomes:
- `holdout_ece` should improve (intraday outcome_z has tighter signal-to-noise than EOD-close proxy).
- Per-tier fits become more reliable (T1 names with active intraday have more meaningful outcomes than days where chain was thin).
- If `holdout_ece` *worsens* significantly, the v10 features may have been picking up signal that correlates with EOD drift rather than intraday move — important to discover before live trading.

Promote bootstrap v2 only if `holdout_ece` improves over bootstrap v1 by ≥5% relative and holdout residual variance does not worsen by >5%.

---

## 6. Streamlit dashboard surface (optional, week 2)

Add a "Bootstrap calibration" tab to [apps/backtest_dashboard.py](../../apps/backtest_dashboard.py). Subsections:

1. **Backfill inventory**: row count by `dryrun_run_id`, date range, outcome attribution coverage % (split by Stage 1 / Stage 2).
2. **Buttons**: launch the five scripts as background jobs (existing `start_job` machinery handles this).
3. **Fit metrics table**: per-fit cv_ece, holdout_ece, residual variance, N, method.
4. **Reliability diagrams**: PNGs from `apps/static/calibration/` (CV view + holdout view).
5. **Promote toggle**: per (feature, tier) row, button to promote bootstrap → live with confirmation modal.
6. **Stage comparison view (NEW)**: side-by-side bootstrap v1 (daily) vs bootstrap v2 (intraday) reliability diagrams.

---

## 7. Risks & mitigations

### 7.1 Look-ahead from late-arriving bhavcopy fields

NSE bhavcopy is end-of-day. If Phase 1+2 replay on date D reads any field whose value reflects post-9:00 IST trading on D itself, that's look-ahead. Mitigations:
- `run_phase1` and `run_phase2` already accept `as_of` and key all queries on it. Audit during Phase A acceptance — sample 3 random replayed dates, hand-check the upstream SQL doesn't read same-day intraday fields.
- All numeric thresholds in Phase 1+2 are derived from prior-day or older data per design.

### 7.2 Mitigated by v2: bandit propensity imputation bias

v1's `COALESCE(propensity, 1/n_arms)` was a band-aid that biased IPS weights without acknowledging it. v2's `propensity_source` flag + 0.3× weight multiplier makes the bias explicit and bounded.

### 7.3 News context leakage

The `published_at ≤ ist(D, 9, 0)` filter must be honored at prompt-build time, not just at retrieval. Mitigation: Phase B acceptance includes a SQL check that no `news_items_used[].published_at > as_of` exists in the backfill UUID. Also: a unit test that exercises `build_phase3_prompt(d, news_cutoff)` with a deliberate future-news item present in the DB and verifies it's excluded.

### 7.4 Anthropic rate-limit overruns

Token-bucket limiter set to 45 req/min (80% of tier 1's 50 req/min ceiling) gives headroom. If Anthropic returns 429, the script retries with exponential backoff via `tenacity` (already in deps).

### 7.5 Cost overrun before completion

`--max-cost` halts the script. Behavior on halt:
- Already-written rows persist with their `(date, candidate_id, batch_uuid)` keys.
- Resume on the next run with the same `--batch-id` skips completed rows.
- Document expected cost ceilings:
  - Pass 1 (30 days): $9 ceiling
  - Pass 2 (180 days): $40 ceiling
  - Total Sonnet budget for backfill: $50 with 25% headroom.

### 7.6 Holdout contamination

If the holdout is too short or has anomalous regime, `holdout_ece` is noisy. Mitigation: holdout default of 15 trading days = ~3 weeks of normal market action. If sensitivity matters, increase via `--holdout-tail-days 25`.

### 7.7 Prompt-injection via historical RSS

Historical RSS items pass through the same `news_sanitizer` the live path uses before being templated into the prompt. Mitigation is the existing live defense, applied to the backfill code path.

---

## 8. Files to add / change

### New (Stage 1)
- `scripts/ensure_historical_prereqs.py` — ~300 LOC (was 250 in v1; +tier + deterministic baseline)
- `scripts/backfill_llm_features.py` — ~250 LOC (was 200; +per-candidate keying + token bucket + news cutoff)
- `scripts/promote_bootstrap_calibration.py` — ~150 LOC (was 100; +holdout scoring)

### New (Stage 2)
- `scripts/reattribute_outcome_z_intraday.py` — ~150 LOC
- Reuses `src/quant/backtest/data_loaders/dhan_historical.py` from the backtest task (no new file needed for the intraday loader itself)

### Schema additions (single migration)
```sql
-- Adds propensity provenance + tier hydration for live rows already in the DB
ALTER TABLE llm_decision_log
    ADD COLUMN propensity_source VARCHAR(20) DEFAULT 'unknown',
    ADD COLUMN news_cutoff_at TIMESTAMPTZ;        -- audit trail of the cutoff used

-- Holdout sentinel (so multiple scripts agree on the holdout window)
CREATE TABLE backfill_holdout_sentinels (
    batch_uuid          UUID NOT NULL,
    holdout_start_date  DATE NOT NULL,
    holdout_end_date    DATE NOT NULL,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (batch_uuid)
);

-- Deterministic baseline universe (Phase 0.5 backfilled in parallel)
CREATE TABLE deterministic_universe_snapshot (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    run_date        DATE NOT NULL,
    instrument_id   UUID NOT NULL REFERENCES instruments(id),
    composite_score NUMERIC(10,6) NOT NULL,
    rank            INT NOT NULL,
    sub_scores      JSONB NOT NULL,    -- six z-scores
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (run_date, instrument_id)
);
CREATE INDEX idx_det_universe_date_rank
    ON deterministic_universe_snapshot(run_date, rank);
```

### Modified (already committed or pending commit)
- `src/config.py` — `laabh_llm_v10_logging_enabled` default `true` (already done)
- `src/fno/thesis_synthesizer.py` — v10 shadow logging gate (already done)
- `src/fno/ml_decision.py` — SQL cast fixes (already done)
- `src/fno/ban_list.py` — NSE CSV `index,symbol` format fix (already done)
- `src/scheduler.py` — `DateTrigger` import + `LAABH_QUANT_RESUME_DATE` (already done)
- `src/quant/orchestrator.py` — per-trade Telegram pings (already done)
- `src/fno/calibration.py` — **NEW** `_load_calibration_rows` reads `propensity_source` and applies 0.3× weight for `'imputed'`; **NEW** holdout-aware fit path

### Modified (new for v2)
- `src/fno/calibration.py` — holdout exclusion + holdout scoring
- `src/fno/llm_outcomes.py` — `--dryrun-run-id` flag; intraday-resolution attribution path
- `src/fno/thesis_synthesizer.py` — accept `news_cutoff` parameter; pass to news fetcher
- `src/fno/news_fetcher.py` (or equivalent) — honor `news_cutoff` at query time

---

## 9. Open decisions for the operator

1. **Promote bootstrap v1 to live scope on Sunday?** Recommendation: **no**, keep as reference. Plan §3 mandates 10-day shadow period; bootstrap is research, not production.

2. **Promote bootstrap v2 (intraday) to live scope?** Recommendation: **only if** `holdout_ece` < 0.05 AND it beats bootstrap v1 by ≥5% relative. Even then, parallel-run for one week with live calibration before considering it the active model.

3. **Haiku vs Sonnet for the backfill?** Sonnet matches the live production model. Using Haiku saves ~6× cost but introduces a confound: bootstrap calibration may not transfer because the feature distributions are model-dependent. **Recommendation: Sonnet** — the $50 cost is trivial vs the validation noise mismatched models introduce.

4. **Run prerequisites against live DB or a separate one?** Live DB, scoped by date filters. Prerequisites are append-only and historical — no conflict with live data.

5. **Dashboard tab now or week 2?** First bootstrap can be inspected via the existing LLM monitor tab + raw SQL. **Recommendation: week 2** unless something feels brittle.

6. **Concurrency limit for Phase A?** Default 4. RBI repo and bhavcopy fetches are mildly rate-bound, but DB writes parallelize well. If logs show DB lock contention, drop to 2.

7. **Stage 2 timing.** Dhan intraday backfill is 20-28 hours. Launch Friday evening; if it's still running at Sunday EOD, defer Stage 2 attribution by a week. Stage 1 fit is still useful on its own.

---

## 10. Cleanup / rollback

If at any point the bootstrap proves untrustworthy:

```sql
-- Nuke all backfill rows
DELETE FROM llm_decision_log WHERE dryrun_run_id = '<BACKFILL_v1_UUID>';
DELETE FROM llm_calibration_models
 WHERE id IN (
   SELECT id FROM llm_calibration_models
   WHERE prompt_version = 'v10_continuous'
     AND fitted_at >= '2026-05-17'
     AND is_active = false   -- never delete an active row
 );
DELETE FROM backfill_holdout_sentinels WHERE batch_uuid = '<BACKFILL_v1_UUID>';
DELETE FROM deterministic_universe_snapshot
 WHERE run_date >= CURRENT_DATE - 180;
```

`price_intraday` rows from Stage 2 are kept regardless — they benefit the backtest harness independent of the bootstrap question.

To kill ongoing live v10 logging (cost emergency):

```
# In .env
LAABH_LLM_V10_LOGGING_ENABLED=false
```
Then restart service. v9 gate decisions continue unaffected.

---

## 11. Net summary

| Deliverable | When | Output |
|---|---|---|
| Live v10 logging | Mon 2026-05-18 onward | ~9 rows/day to `llm_decision_log` (live scope) |
| Phase A (prereqs) | Sat 2026-05-16 | 180 days × {bhavcopy, iv_history, vol_surface, regime, Phase 1+2, tier, deterministic baseline} |
| Phase B (LLM replay) | Sun 2026-05-17 morning | ~1500 v10 rows under BACKFILL_v1 UUID |
| Phase C (EOD attribution) | Sun 2026-05-17 afternoon | ≥60% rows with `counterfactual_eod` outcome |
| Phase D (Stage 1 fit) | Sun 2026-05-17 evening | 4-6 calibration models, `holdout_ece` reported |
| Phase E (decision) | Sun 2026-05-17 22:00 IST | Promote / hold decision; no live mode flip |
| Phase F (Dhan intraday) | Fri 2026-05-15 → Sun 2026-05-24 | `price_intraday` populated, 180 days × ~200 instruments |
| Phase G (intraday attribution) | Sun 2026-05-24 | Stage 1 rows refitted with `counterfactual_intraday` |
| Phase H (Stage 2 fit) | Sun 2026-05-24 | bootstrap_v2 calibration models |

Total LLM spend: ~$50 (Sonnet, one-time). Total Dhan calls: ~50K (within procured subscription quota). Total wall clock: Phase A 1.5-3 hr, Phase B 4-6 hr, Phase C-E 2 hr, Phase F-H 20-28 hr (background).