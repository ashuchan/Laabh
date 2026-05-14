# Implementation Plan — LLM as Feature Generator + Confidence Calibration (v2)

*Scope: replace the PROCEED/SKIP/HEDGE gate at [thesis_synthesizer.py:117-216](../../src/fno/thesis_synthesizer.py#L117-L216) with continuous LLM-derived features that feed a calibrated **bandit context vector**, not a separate sizing multiplier. Total: ~5 weeks of work (added Phase 0.5 deterministic baseline), gated by data accumulation.*

**v2 changes vs v1** (applied from the senior-architect review): schema FKs typed correctly (C1); backfill scope clarified (C2); single LLM entry point at the bandit context vector (C3); outcome attribution on position-close (C5); calibration target switched to a continuous z-scored outcome with Platt→isotonic ladder and walk-forward CV (S1–S3); IPS reweighting via logged bandit posteriors (S4); deterministic baseline added as Phase 0.5 (O4). Known limitations enumerated rather than papered over (S6, S7, O1–O6).

---

## Design principles (non-negotiable)

1. **One LLM entry point.** Continuous LLM features enter the bandit's context vector. The bandit's posterior mean — not a separate hand-coded multiplier — drives sizing. No double-counting, no parallel score-blends.
2. **No big-bang cutover.** New system runs in shadow alongside the gate for ≥10 trading days. Compare distributions, not point estimates.
3. **Calibration needs data we don't have yet.** First ~4 weeks = collect; weeks 4+ = fit; week 5+ = act on calibrated outputs. The plan is sequenced around this.
4. **Deterministic baseline is the null hypothesis.** Phase 0.5 ships a six-factor scorer in parallel. The LLM has to beat it on Sharpe to justify its cost.
5. **Survivorship-aware calibration.** Calibrate against logged bandit posteriors via inverse-propensity-score (IPS) weighting — the only honest way to use trades that were filtered through downstream gates.
6. **Every new function follows the project rule** in [CLAUDE.md](../../CLAUDE.md): accept `as_of: datetime | None = None` and `dryrun_run_id: uuid.UUID | None = None`.

---

## Phase 0 — Data layer & logging (Week 1, ~3 days)

**Goal:** Capture every LLM call's raw output and the eventual outcome — without changing decisions.

### 0.1 New migration: `database/migrations/2026_05_15_llm_features.sql`

```sql
-- One row per (run_date, instrument_id, llm_call). Stores raw + parsed LLM output.
CREATE TABLE llm_decision_log (
    id BIGSERIAL PRIMARY KEY,
    run_date DATE NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    dryrun_run_id UUID NULL,
    instrument_id UUID NOT NULL REFERENCES instruments(id),
    phase TEXT NOT NULL,                   -- 'fno_thesis' | 'quant_universe'
    prompt_version TEXT NOT NULL,          -- 'v9' | 'v10_continuous'
    model_id TEXT NOT NULL,                -- pinned snapshot, e.g. 'claude-sonnet-4-20250514'

    -- Legacy categorical (kept for backwards compat in shadow phase)
    decision_label TEXT NULL,              -- PROCEED | SKIP | HEDGE | NULL

    -- Continuous features (Phase 1 will populate; NULL for legacy v9-only rows)
    directional_conviction REAL NULL,      -- [-1, +1]
    thesis_durability REAL NULL,           -- [0, 1]
    catalyst_specificity REAL NULL,        -- [0, 1]
    risk_flag REAL NULL,                   -- [-1, 0]
    raw_confidence REAL NULL,              -- model's self-stated probability [0, 1]

    -- Calibrated values (Phase 2 will populate)
    calibrated_conviction REAL NULL,
    calibration_model_id INT NULL,         -- FK to llm_calibration_models

    -- Outcome (filled on position close — NOT end-of-day; see §0.3)
    outcome_pnl_pct REAL NULL,
    outcome_z REAL NULL,                   -- pnl_pct / expected_vol_at_entry; the calibration target
    outcome_class TEXT NULL,               -- 'traded' | 'counterfactual' | 'unobservable' | 'timeout'
    bandit_posterior_mean REAL NULL,       -- copied from quant_signal_log at decision time (IPS weight input)
    bandit_posterior_var REAL NULL,
    bandit_arm_propensity REAL NULL,       -- derived: P(this arm chosen | context)
    outcome_attributed_at TIMESTAMPTZ NULL,

    -- Full LLM payload for audit / re-parse
    raw_response JSONB NOT NULL,

    UNIQUE (run_date, instrument_id, phase, prompt_version, dryrun_run_id)
);

CREATE INDEX idx_llm_log_outcome_pending ON llm_decision_log(outcome_attributed_at)
    WHERE outcome_attributed_at IS NULL;
CREATE INDEX idx_llm_log_calibration_ready ON llm_decision_log(prompt_version, phase, outcome_attributed_at)
    WHERE outcome_z IS NOT NULL AND outcome_class IN ('traded', 'counterfactual');

-- Convert to TimescaleDB hypertable if available; otherwise plain table (O1).
DO $$ BEGIN
    PERFORM create_hypertable('llm_decision_log', 'as_of',
        chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE);
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'TimescaleDB unavailable — llm_decision_log is a plain table';
END $$;

-- Retention: 2 years. Calibration uses rolling 90-day window anyway.
-- (Add policy in Phase 0.1 follow-up via add_retention_policy if hypertable conversion succeeded.)

-- Versioned calibration curves (one row = one fitted model).
CREATE TABLE llm_calibration_models (
    id SERIAL PRIMARY KEY,
    fitted_at TIMESTAMPTZ NOT NULL,
    prompt_version TEXT NOT NULL,
    phase TEXT NOT NULL,
    feature_name TEXT NOT NULL,            -- 'directional_conviction' | 'raw_confidence'
    instrument_tier TEXT NOT NULL,         -- 'T1' | 'T2' | 'pooled' (O3)
    method TEXT NOT NULL,                  -- 'platt' (small N) | 'isotonic' (large N)
    n_observations INT NOT NULL,
    params JSONB NOT NULL,                 -- Platt: {a, b}; Isotonic: {x_knots, y_knots}
    cv_ece REAL NULL,                      -- expected calibration error, walk-forward (S1)
    cv_residual_var REAL NULL,             -- Var(outcome_z | calibrated) (S1)
    is_active BOOLEAN DEFAULT FALSE,
    UNIQUE (prompt_version, phase, feature_name, instrument_tier, fitted_at)
);
```

**Schema corrections vs v1:** `instrument_id` is `UUID` (matches [`instruments.id`](../../database/schema.sql) and [`fno_candidates.instrument_id`](../../database/schema.sql#L890)). Hypertable + retention policy added per O1.

### 0.2 Backfill — what's actually recoverable

[`fno_candidates`](../../database/schema.sql#L890-L913) stores only `llm_thesis TEXT` and `llm_decision VARCHAR(10)` — **no raw confidence, no continuous scores, no JSONB response.** Backfill therefore:

- **Recovers:** `decision_label`, `instrument_id`, `run_date`, `prompt_version='v9'`, free-text thesis (into `raw_response.thesis_text`).
- **Cannot recover:** `raw_confidence`, the four continuous fields, anything not stored.

Consequence: there is no head-start on the continuous-feature calibration data. The 25-trading-day count to first active calibration model starts at Phase 1 cutover, not Phase 0. The plan's calendar reflects this.

### 0.3 Outcome-attribution job — triggered on position close, not cron (C5)

Add to [`src/scheduler.py`](../../src/scheduler.py): a listener (or a 5-minute polling job) `attribute_llm_outcomes()` that:

- Watches `fno_signals` for `status` transitions to `'closed'`. For each closed signal, find the upstream `llm_decision_log` row via `candidate_id` lineage, then write:
  - `outcome_pnl_pct = (close_price - entry_price) / entry_price`
  - `outcome_z = outcome_pnl_pct / expected_vol_at_entry` (the calibration target — see S3)
  - `outcome_class = 'traded'`
- For un-traded SKIPs (`status IS NULL` after T+5 trading days): attempt counterfactual P&L from [`options_chain`](../../database/schema.sql#L830) snapshots at the proposed strike. If strikes are missing or thinly quoted (spread > 10%, no quotes for 30+ min around proposed entry), mark `outcome_class = 'unobservable'` and exclude from calibration (S6).
- For positions still open at T+30 trading days: mark `outcome_class = 'timeout'`, exclude.

`expected_vol_at_entry` comes from the existing [`feature_store`](../../src/quant/feature_store.py) `rv_30min` annualized; for F&O scope, scale by sqrt of expected holding period (durability score in days × √(1/252)).

### 0.4 Wire shadow logging into the existing path

Modify [`thesis_synthesizer.py`](../../src/fno/thesis_synthesizer.py) to write every v9 call to `llm_decision_log` (with continuous fields NULL, decision_label populated). **No decision logic changes in Phase 0.**

**Deliverable check (5 days):** `SELECT COUNT(*), prompt_version FROM llm_decision_log GROUP BY prompt_version` returns 50–150 rows/day; outcome-attribution job is running cleanly with zero `outcome_class='timeout'` entries (because nothing should be aging out in week 1).

---

## Phase 0.5 — Deterministic baseline (Week 1–2, parallel; ~3 days) (O4)

**Goal:** Build the null hypothesis. The LLM has to beat this on Sharpe to justify its cost.

### 0.5.1 Six-factor composite (deterministic only)

New module `src/fno/deterministic_universe.py`. Compute, per F&O underlying, six z-scored sub-scores using the 60-day rolling distribution of each:

1. **Liquidity** — `z(20d_ADV) + z(-mean_spread_bps) + z(OI_persistence_30d)`
2. **IV-rank momentum** — `IV_rank_252d × ΔIV_rank_5d`
3. **Realized-vol regime** — `decile(RV_20d) × sign(RV_20d - RV_60d)`
4. **Trend strength** — `z((P - SMA_50)/ATR_20) × OBV_slope`
5. **Mean-reversion stretch** — `|RSI_14 - 50|/50 × (P - VWAP)/VWAP × -1`
6. **Microstructure** — `gap_z × pre_open_OI_change × PCR_z`

Inputs already exist: [`price_daily`](../../database/schema.sql), [`options_chain`](../../database/schema.sql#L830), [`iv_history`](../../database/migrations/2026_05_13_vrp_engine.sql), [`vol_surface_snapshot`](../../database/migrations/2026_05_13_vrp_engine.sql).

**Composite v0:** equal-weighted sum across the six z-scores. Top-K = 25 by composite.
**Composite v1 (Week 4+):** PCA loadings re-fit monthly on rolling 90-day information coefficient (IC = rank correlation between sub-score and next-day Sharpe).

### 0.5.2 Shadow execution

Write deterministic top-K daily to `quant_universe_baseline` (new lightweight table). Compare daily overlap with Phase-2 passers and (later) with LLM-feature-driven selections.

**Success criterion (90-day window in Phase 5):** LLM-features Sharpe ≥ deterministic Sharpe + 0.15. If equal or worse, the LLM doesn't pay for its cost.

---

## Phase 1 — Continuous LLM output (Week 2, ~4 days)

**Goal:** Switch Claude to emit continuous scores. Still no decision change — runs in shadow.

### 1.1 New prompt `FNO_THESIS_SYSTEM_V10` in [`src/fno/prompts.py`](../../src/fno/prompts.py)

Keep v9 intact for rollback. v10:

- Same context blocks (catalyst, VRP, surface, regime).
- **Output schema is continuous-only**:

```json
{
  "directional_conviction": -0.4,
  "thesis_durability": 0.65,
  "catalyst_specificity": 0.8,
  "risk_flag": -0.2,
  "raw_confidence": 0.58,
  "proposed_structure": "bull_call_spread_19500_19700",
  "proposed_strikes": [19500, 19700],
  "proposed_expiry": "2026-05-22",
  "reasoning_oneline": "Q4 results May 16, IV rank 78, term structure flat"
}
```

Note `proposed_strikes` and `proposed_expiry` are now **structured fields**, not embedded in free text — required for counterfactual P&L computation (S6).

- **Critical prompt instruction:** "Do not refuse to score. Weak conviction → small magnitude. The sizing layer handles low-conviction trades; your job is to score, not to gate."

### 1.2 Parser + validator: `src/fno/llm_features.py`

```python
@dataclass
class LLMFeatureScore:
    directional_conviction: float   # clipped [-1, 1]
    thesis_durability: float        # clipped [0, 1]
    catalyst_specificity: float     # clipped [0, 1]
    risk_flag: float                # clipped [-1, 0]
    raw_confidence: float           # clipped [0, 1]
    proposed_strikes: list[float] | None
    proposed_expiry: date | None
    proposed_structure: str | None

def parse_llm_features(
    raw_response: dict,
    *,
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> LLMFeatureScore | None:
    ...
```

Reject responses where all four numeric fields are zero (degenerate / lazy LLM output); fall back to v9 with a logged warning.

### 1.3 Shadow execution — fire-and-forget (C4)

```python
# In thesis_synthesizer.py:
v9_decision = await call_claude_v9(prompt)              # blocking, production path
asyncio.create_task(_log_v10_shadow(prompt))            # fire-and-forget; bounded timeout inside
return v9_decision
```

`_log_v10_shadow` has its own timeout (30s), retries (1), and exception swallowing. Production v9 latency is unaffected.

### 1.4 Diagnostic — derived label agreement matrix

Helper that derives synthetic PROCEED/SKIP/HEDGE from v10 output (`PROCEED if |conviction| > 0.4 and durability > 0.5`). Build a 3×3 agreement matrix v9 vs synthetic-v10 over 10 trading days. If v10 derives PROCEED on >50% of v9-SKIP names, the gate is genuinely over-rejecting (as hypothesized).

---

## Phase 2 — Calibration pipeline (Week 3, ~4 days; runs weekly thereafter)

**Goal:** Map raw LLM scores → conditional expected `outcome_z`. Re-fit weekly. **Calibration target is the continuous z-scored outcome, not a binary hit (S3).**

### 2.1 Method ladder: Platt → isotonic (S2)

`src/fno/calibration.py`:

```python
def fit_calibration(
    feature_name: str,
    prompt_version: str,
    phase: str,
    instrument_tier: str = 'pooled',          # 'T1' | 'T2' | 'pooled' (O3)
    *,
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> CalibrationModel | None:
    """Pull (raw_score, outcome_z, ips_weight) tuples; fit method by N."""
    rows = _load_calibration_rows(...)
    if len(rows) < 100:
        return None
    method = 'platt' if len(rows) < 500 else 'isotonic'
    ...
```

- **N < 100:** no fit, no model active.
- **100 ≤ N < 500:** Platt scaling — 2-parameter logistic on z-target with sign-corrected conviction. Robust to small N.
- **N ≥ 500:** Isotonic regression. Free to bend non-monotonically across local regions but constrained globally.

### 2.2 Sample weights = IPS reweighting (S4)

The bandit logs `posterior_mean` and `posterior_var` per arm per decision ([orchestrator.py:703,729](../../src/quant/orchestrator.py#L703)). Derive a softmax-equivalent propensity at decision time:

```python
# In the bandit selector at choice time, also log:
#   propensity = exp(post_mean / tau) / sum_j(exp(post_mean_j / tau))
# where tau ≈ sqrt(post_var) (Thompson temperature analog)
```

In calibration, weight each observation by `1 / propensity` (with clipping at [0.1, 10] to bound variance), with an additional 0.3× multiplier for `outcome_class='counterfactual'` rows. This corrects for survivorship: rare arm choices are upweighted, dominant arm choices are downweighted.

### 2.3 Walk-forward cross-validation (S1, S2)

No single 80/20 split. Use **expanding-window walk-forward folds** with 1-day embargo:

```
fold 1: train [day 1 .. day N-30], test [day N-29 .. day N-20]
fold 2: train [day 1 .. day N-20], test [day N-19 .. day N-10]
fold 3: train [day 1 .. day N-10], test [day N-9 .. day N]
```

Aggregate metrics across folds.

### 2.4 Metrics (S1)

Compute and persist:
- **`cv_ece`** — Expected Calibration Error: bin predictions into 10 deciles, compute weighted mean of `|mean_predicted - mean_realized|`. Lower = better.
- **`cv_residual_var`** — `Var(outcome_z - f̂(conviction))`. Sharper signal = lower residual variance.
- **Reliability diagram** — produced as a PNG per fit, stored to `apps/static/calibration/<fitted_at>.png` for the dashboard.

A new model goes active only if **both** ECE improves by ≥5% AND residual variance does not worsen by >5% vs current active. Otherwise keep current.

### 2.5 Weekly scheduled job

Sundays 22:00 IST in [`src/scheduler.py`](../../src/scheduler.py). Fit per (feature, prompt_version, phase, instrument_tier). Stratify by tier (O3) — T1 and T2 instruments have structurally different hit-rate distributions; pooled fit is the fallback when per-tier N < 100.

**Deliverable check:** First active model at ~T+25 trading days from Phase 1 cutover. Target ECE ≤ 0.08 (well-calibrated baseline ≈ 0.05–0.10).

---

## Phase 3 — Bandit cutover (Week 4, ~3 days) (C3)

**Goal:** LLM features enter the bandit context vector. The bandit's posterior mean drives sizing. The categorical gate is removed.

### 3.1 Extend LinTS context vector at [`bandit/lints.py`](../../src/quant/bandit/lints.py)

Today's context vector (from feature_store): IV, RV percentile, time-of-day, running P&L %. Add four LLM-derived dimensions:

```python
context = np.concatenate([
    deterministic_features,           # existing dimensions
    [
        calibrated_conviction,        # from llm_decision_log
        thesis_durability,
        catalyst_specificity,
        risk_flag,
    ],
])
```

The bandit *learns* the coefficients on these via standard LinTS update. If the LLM signal is noise, the learned coefficients collapse to near-zero and the system degrades gracefully to deterministic-only. **No human-coded multiplier; no separate score blend.** This is the single LLM entry point.

### 3.2 Phase 3 gate removal in [`src/fno/orchestrator.py`](../../src/fno/orchestrator.py)

- Remove the filter that drops non-PROCEED rows from Phase 3.
- All Phase-2 passers proceed; the bandit at the quant orchestrator decides per-tick which arm to play.
- v10 LLM call still happens, but its outputs are features, not gates.

### 3.3 Hard guards untouched

Deterministic gates remain (kill-switch, capacity, warmup, liquidity Phase 1, margin). LLM never modulates the **risk** layer, only the **expected-value** estimate inside the bandit.

**Boundary test (must pass before cutover):** synthetic test injects `calibrated_conviction=10.0` (out-of-distribution high). Sized lots must still be capped by capacity and Kelly bounds. If this test fails, hard guards are accidentally bypassed.

### 3.4 Bandit-aware exploration replaces fixed 15% (S7)

Drop the fixed exploration rate. LinTS already does Thompson sampling — exploration is *native*, parameterized by posterior variance. Add only one tweak: **reserve 1 capacity slot** for high-posterior-variance arms (O2), so exploration trades don't crowd out high-conviction trades.

**Deliverable check:** post-cutover, daily trade count rises from ~5–15 to ~25–40. Per-position lot size drops 30–50% (smaller, more numerous bets — the bandit is doing its job).

---

## Phase 4 — Monitoring & validation (Week 5+, ongoing)

### 4.1 Dashboards in [`apps/backtest_dashboard.py`](../../apps/backtest_dashboard.py)

- **Reliability diagram** — predicted vs realized `outcome_z` in 10 buckets, per active model. Diagonal = perfect.
- **Feature drift** — rolling weekly mean of each continuous LLM output. Alarm if any feature shifts >0.15 month-over-month (prompt regression suspect).
- **Bandit coefficient stability** — plot LinTS posterior means on the 4 LLM dimensions over time. If they trend toward zero, LLM adds no edge.
- **Three-way P&L compare** — v9-gate shadow vs v10-feature live vs deterministic-baseline (Phase 0.5). 30-day rolling Sharpe each.

### 4.2 Rollback triggers — `LAABH_LLM_MODE=gate|feature|shadow`

If any fire, revert to v9 gate (which is stateless — just thresholds in [`prompts.py`](../../src/fno/prompts.py) — so rollback is one env var + restart; O6):

| Trigger | Threshold |
|---|---|
| 30-day Sharpe (v10) < 0.7× 30-day Sharpe (deterministic baseline) | hard rollback |
| Max drawdown (v10) > 1.5× v9 shadow drawdown | hard rollback |
| Calibration ECE > 0.15 across all active models | hard rollback |
| Bandit LLM-dim coefficients all within ±2σ of zero for 20 trading days | soft rollback — LLM is noise |
| LLM cost per trade > 3× v9 baseline | review, not auto-rollback |

### 4.3 Success metrics (90-day window)

| Metric | v9 gate | v10 features | Deterministic (Phase 0.5) |
|---|---|---|---|
| Daily trade count | 5–15 | **25–40** | 25 (top-K fixed) |
| Win rate | ~52% | ≥48% acceptable | benchmark |
| Annualized Sharpe | baseline | **≥ deterministic + 0.15** | benchmark |
| Max drawdown | baseline | ≤ 1.2× baseline | benchmark |
| Capital deployed daily | ~30% | 50–70% | 50–70% |

**The decisive metric** is v10 Sharpe vs deterministic Sharpe (O4). If v10 doesn't beat deterministic by 0.15, the LLM does not justify its API spend.

---

## Known limitations — accepted, not fixed

| ID | Limitation | Mitigation in v2 |
|---|---|---|
| S5 | LLM output dimensions are correlated; we treat them as independent features in the bandit context | LinTS handles correlated features via `A_inv` cross-terms; sufficient for current scale. Joint multivariate calibration deferred to v3 if N allows. |
| S6 | Counterfactual P&L is unobservable for un-quoted strikes | `outcome_class='unobservable'` excludes these rows from calibration |
| O3 | Per-tier calibration may be N-starved | Pooled fallback at the (prompt, phase, feature) level |
| O5 | LLM cost is unquantified | First action of Phase 0: measure baseline calls/day × $/call; require explicit user authorization before Phase 1 doubles it |
| O6 | Model-pinning required | `model_id` column persists the snapshot; calibration is keyed on it implicitly via `prompt_version` |
| Cost cap | Doubled cost during Phase 1 shadow | 2-week window only; can be cut short if v10 derived labels match v9 on >80% of rows |

---

## Sequencing summary

| Week | Phase | Deliverable | Advance gate |
|---|---|---|---|
| 1 | 0 — Plumbing | Migration applied, shadow logging live, outcome-on-close job running | 5 days of clean shadow data |
| 1–2 | 0.5 — Deterministic baseline | Six-factor scorer running, daily top-K persisted | Top-K stable, overlap with Phase-2 logged |
| 2 | 1 — v10 prompt shadow | v10 fires fire-and-forget, both logged | 10 days of v9/v10 paired data |
| 3 | 2 — Calibration | Platt fits successfully on synthetic data; walk-forward CV; weekly job scheduled | Sklearn integrated, first real fit ECE < 0.12 |
| 4 | 3 — Bandit cutover | Gate removed; LLM features in LinTS context; boundary test passes | First active calibration model exists; deterministic baseline > 30 days of data |
| 5+ | 4 — Monitor | Dashboards live, rollback path tested; three-way Sharpe compare | — |

---

**Suggested first step:** Phase 0 plumbing + Phase 0.5 deterministic baseline run in parallel during Week 1. Both are strictly additive (no decision changes, no live-path risk). The deterministic baseline starts accumulating its own track record from day 1, so the eventual LLM-vs-deterministic comparison has a real 90-day window by the time Phase 3 cuts over.
