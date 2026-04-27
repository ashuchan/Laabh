# CLAUDE-PHASE-FNO.md — F&O Intraday Intelligence Module (Hybrid Workflow POC)

## Overview

This phase implements the **hybrid four-phase F&O intraday workflow** as a parallel
module layered on top of Phases 1–3. It mirrors the pre-market and intraday workflow
of an experienced F&O day trader: filter the universe, score catalysts, build trade
theses, and manage the intraday loop with disciplined exits.

* **Universe**: full F&O-eligible universe — all ~200 stock options + all index options.
  Phase 1 of the workflow does the heavy filtering down to ~10 candidates.
* **Run mode**: forward live, with Telegram notifications + DB persistence. No backtest
  harness in this POC (added later).
* **Strategy universe**: long calls, long puts, debit spreads, credit spreads. Iron
  condors and straddles deferred.
* **Self-learning loop**: every Phase 3 thesis is auto-paper-traded via the existing
  `signal_auto_trades` mechanism. Strike-ranker weights are versioned but not
  auto-tuned in this POC — manual tuning only, with point-in-time correctness
  preserved for future regression.

## Prerequisites

* Phase 1 fully functional: instruments table populated, Angel One auth working,
  RSS pipeline running, `signals` and `raw_content` tables flowing.
* Phase 2 fully functional: paper trading engine, `signal_auto_trades`, analyst
  scoreboard with at least 2 weeks of resolved signals.
* Phase 3 partially required: convergence engine MUST be working. Whisper pipeline
  is NICE TO HAVE — F&O can launch without live transcripts, just RSS/news.
* Existing tables this phase reads from:
  `instruments`, `price_ticks`, `price_daily`, `signals`, `analyst_signals`,
  `signal_auto_trades`, `data_sources`, `analysts`, `system_config`, `notifications`,
  `llm_audit_log` (shared with Phase 1 — see Shared Infrastructure below).

### Current repository state (April 2026)

The repo currently contains specification documents and the schema only — **no
backend Python code has been implemented yet**. Anyone working on this phase must
first ensure Phases 1–3 are implemented per their respective specs:

* `CLAUDE.md` — Phase 1: data collection (collectors, extraction, models, scheduler)
* `CLAUDE-PHASE2.md` — Phase 2: paper trading + analyst scoreboard + REST API
* `CLAUDE-PHASE3.md` — Phase 3: convergence engine + technical confirmation
  (the Whisper sub-pipeline can be deferred — F&O does not depend on it)

If those phases are not yet implemented, build them first using their respective
docs as the contract. Do not start F&O work until the prerequisites listed above
are functional and tests pass.

## Shared Infrastructure — `llm_audit_log`

Every LLM call across the system (Phase 1 signal extraction, Phase 3 thesis
synthesis, any future LLM use) writes a row to a single `llm_audit_log` table
before returning to the caller. This is non-negotiable — it is the only way to
replay decisions, audit prompt changes, and run regression tests on deterministic
LLM stages.

The table is created as part of Phase 1's schema migration (see Task 1) and is
referenced — never re-created — by Phase 3's thesis synthesizer and any future
LLM consumer.

```sql
CREATE TABLE llm_audit_log (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    caller          VARCHAR(50) NOT NULL,         -- "phase1.extractor" / "fno.thesis" / etc.
    caller_ref_id   UUID,                         -- optional FK to caller's row
                                                  -- (raw_content.id, fno_candidates.id, ...)
    model           VARCHAR(50) NOT NULL,
    temperature     NUMERIC(4,2) NOT NULL,
    prompt          TEXT NOT NULL,
    response        TEXT NOT NULL,
    response_parsed JSONB,                        -- structured output if applicable
    tokens_in       INT,
    tokens_out      INT,
    latency_ms      INT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_llm_audit_caller ON llm_audit_log(caller, created_at DESC);
CREATE INDEX idx_llm_audit_caller_ref ON llm_audit_log(caller_ref_id);
```

Phase 1's `src/extraction/llm_extractor.py` MUST write to this table on every
Claude API call. Same for `src/fno/thesis_synthesizer.py` in Task 8 below.

## Critical Corrections From Prior Workflow Review

These three items override anything in earlier design notes. Get them right before
writing any other code.

### 1. Expiry calendar (SEBI Sept-2025 reform)

* **NSE Nifty 50 weekly expiry: Tuesday** (NOT Thursday — that was the old cycle)
* **BSE Sensex weekly expiry: Thursday**
* **Bank Nifty / Fin Nifty / Midcap Nifty: monthly only**, last Tuesday of the month.
  Weekly expiries for these were discontinued on 2024-11-20.
* **All NSE monthly contracts: last Tuesday** (was last Thursday)
* If a Tuesday/Thursday is a market holiday, the expiry shifts to the **previous
  trading day** — never the next.
* The system MUST source the expiry calendar dynamically from NSE/BSE bhavcopy or
  Angel One instrument master, never hard-code expiry days.

### 2. F&O ban list (MWPL > 95%)

* SEBI publishes a daily "securities in F&O ban period" list. Trading new positions
  in these names is restricted to closing existing ones.
* The system MUST fetch this list daily at 6 PM IST from
  `https://nsearchives.nseindia.com/archives/fo/sec_ban/fo_secban_<DDMMYYYY>.csv`
  (URL pattern; verify exact path at implementation time) and exclude banned
  instruments from Phase 1 universe filtering.
* Store in `fno_ban_list` table (DDL below).

### 3. India VIX regime gate

* India VIX is the volatility regime variable. Strategy mix MUST change with VIX:
  * VIX < 12: low-vol regime → favor premium selling (credit spreads), avoid long gamma
  * VIX 12–18: neutral regime → standard playbook (long calls / long puts on directional)
  * VIX > 18: high-vol regime → favor debit spreads (defined-risk), penalize naked option buying
* Fetch India VIX value via Angel One SmartAPI (token: `26017` for `INDIA VIX` index).
* Re-evaluate regime every 5 minutes during market hours.

## Trading Universe Scope

Universe = all instruments where `instruments.is_fno = true` AND `instruments.segment IN
('NSE_FO', 'BSE_FO')`. At each Phase 1 run:

1. Start with all ~200 F&O-eligible stocks + 5 indices (Nifty 50, Sensex, Bank Nifty,
   Fin Nifty, Midcap Nifty).
2. Exclude instruments in today's `fno_ban_list`.
3. Exclude instruments with no chain data in the last 24 hours (data quality gate).
4. Phase 1 funnel: ~205 → 50 → 20 → 10.

## New Database Schema

Add to `database/schema.sql` and create as an Alembic migration named
`add_fno_intelligence_module`. Use the same conventions as existing schema (UUIDs,
TIMESTAMPTZ, snake_case, references with ON DELETE CASCADE where appropriate).

```sql
-- ============================================================================
-- F&O Intelligence Module — new tables
-- ============================================================================

-- Daily F&O ban list (SEBI MWPL>95%)
CREATE TABLE fno_ban_list (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    instrument_id   UUID NOT NULL REFERENCES instruments(id),
    ban_date        DATE NOT NULL,
    source          VARCHAR(20) DEFAULT 'NSE',
    fetched_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (instrument_id, ban_date, source)
);
CREATE INDEX idx_fno_ban_date ON fno_ban_list(ban_date);

-- Options chain snapshots (TimescaleDB hypertable, partitioned by snapshot_at)
CREATE TABLE options_chain (
    instrument_id     UUID NOT NULL REFERENCES instruments(id),  -- the underlying
    snapshot_at       TIMESTAMPTZ NOT NULL,
    expiry_date       DATE NOT NULL,
    strike_price      NUMERIC(12,2) NOT NULL,
    option_type       CHAR(2) NOT NULL CHECK (option_type IN ('CE','PE')),
    -- pricing
    ltp               NUMERIC(12,2),
    bid_price         NUMERIC(12,2),
    ask_price         NUMERIC(12,2),
    bid_qty           INT,
    ask_qty           INT,
    -- volume + OI
    volume            BIGINT,
    oi                BIGINT,
    oi_change         BIGINT,                    -- vs previous snapshot
    -- greeks (computed if not provided by feed)
    iv                NUMERIC(8,4),              -- implied volatility (annualized %)
    delta             NUMERIC(8,4),
    gamma             NUMERIC(10,6),
    theta             NUMERIC(10,4),
    vega              NUMERIC(10,4),
    -- underlying snapshot
    underlying_ltp    NUMERIC(12,2),
    PRIMARY KEY (instrument_id, snapshot_at, expiry_date, strike_price, option_type)
);
SELECT create_hypertable('options_chain', 'snapshot_at',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);
CREATE INDEX idx_options_chain_underlying_expiry
    ON options_chain(instrument_id, expiry_date, snapshot_at DESC);

-- Daily IV percentile per instrument (for IV regime classification)
CREATE TABLE iv_history (
    instrument_id     UUID NOT NULL REFERENCES instruments(id),
    date              DATE NOT NULL,
    atm_iv            NUMERIC(8,4) NOT NULL,
    iv_rank_52w       NUMERIC(6,2),    -- 0-100, where 100 = 52w high
    iv_percentile_52w NUMERIC(6,2),    -- 0-100, % of days IV was below today
    PRIMARY KEY (instrument_id, date)
);

-- India VIX time series (separate hypertable, lightweight)
CREATE TABLE vix_ticks (
    timestamp         TIMESTAMPTZ NOT NULL,
    vix_value         NUMERIC(8,4) NOT NULL,
    regime            VARCHAR(10) NOT NULL CHECK (regime IN ('low','neutral','high')),
    PRIMARY KEY (timestamp)
);
SELECT create_hypertable('vix_ticks', 'timestamp',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE
);

-- Phase 1/2/3 candidate snapshots (one row per instrument per filter run)
CREATE TABLE fno_candidates (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    instrument_id     UUID NOT NULL REFERENCES instruments(id),
    run_date          DATE NOT NULL,
    phase             INT NOT NULL CHECK (phase IN (1,2,3)),
    -- Phase 1 outputs
    passed_liquidity  BOOLEAN,
    atm_oi            BIGINT,
    atm_spread_pct    NUMERIC(6,4),
    avg_volume_5d     BIGINT,
    -- Phase 2 outputs
    news_score        NUMERIC(4,2),    -- 0-5
    sentiment_score   NUMERIC(4,2),    -- -5 to +5 (signed)
    fii_dii_score     NUMERIC(4,2),    -- 0-5
    macro_align_score NUMERIC(4,2),    -- 0-5
    convergence_score NUMERIC(4,2),    -- from existing engine
    composite_score   NUMERIC(6,2),
    -- Phase 3 outputs
    technical_pass    BOOLEAN,
    iv_regime         VARCHAR(15),     -- cheap / neutral / expensive
    oi_structure      VARCHAR(20),     -- clean_walls / mixed / no_signal
    llm_thesis        TEXT,            -- one-line thesis if passed
    llm_decision      VARCHAR(10),     -- pass / skip
    config_version    VARCHAR(20),     -- which config produced this row
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (instrument_id, run_date, phase)
);
CREATE INDEX idx_fno_candidates_run ON fno_candidates(run_date, phase, composite_score DESC);

-- F&O signals — strike-level recommendations
CREATE TABLE fno_signals (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    underlying_id       UUID NOT NULL REFERENCES instruments(id),
    candidate_id        UUID REFERENCES fno_candidates(id),
    -- contract spec
    strategy_type       VARCHAR(20) NOT NULL,    -- long_call, long_put, debit_call_spread,
                                                  -- debit_put_spread, credit_call_spread,
                                                  -- credit_put_spread
    expiry_date         DATE NOT NULL,
    legs                JSONB NOT NULL,          -- array of {strike, option_type, action, qty_lots}
    -- entry plan
    entry_premium_net   NUMERIC(12,2),           -- net debit/credit per lot
    target_premium_net  NUMERIC(12,2),
    stop_premium_net    NUMERIC(12,2),
    max_loss            NUMERIC(12,2),           -- defined-risk strategies only
    max_profit          NUMERIC(12,2),
    breakeven_price     NUMERIC(12,2),
    -- ranker fields
    ranker_score        NUMERIC(6,2),
    ranker_breakdown    JSONB,                   -- per-dimension scores
    ranker_version      VARCHAR(20),
    -- regime context
    iv_regime_at_entry  VARCHAR(15),
    vix_at_entry        NUMERIC(8,4),
    -- lifecycle
    status              VARCHAR(15) DEFAULT 'proposed',
                        -- proposed, paper_filled, active, scaled_out_50, closed_target,
                        -- closed_stop, closed_time, rejected_risk
    proposed_at         TIMESTAMPTZ DEFAULT NOW(),
    filled_at           TIMESTAMPTZ,
    closed_at           TIMESTAMPTZ,
    final_pnl           NUMERIC(12,2),
    notes               TEXT
);
CREATE INDEX idx_fno_signals_status ON fno_signals(status);
CREATE INDEX idx_fno_signals_underlying ON fno_signals(underlying_id, proposed_at DESC);

-- Strike ranker config history (for regression of weight changes against outcomes)
CREATE TABLE ranker_configs (
    version           VARCHAR(20) PRIMARY KEY,
    weights           JSONB NOT NULL,            -- {directional, convergence, iv_value,
                                                  --  theta, oi_structure, liquidity}
    activated_at      TIMESTAMPTZ DEFAULT NOW(),
    deactivated_at    TIMESTAMPTZ,
    notes             TEXT
);

-- F&O cooldown tracker (revenge-trade prevention)
CREATE TABLE fno_cooldowns (
    underlying_id     UUID NOT NULL REFERENCES instruments(id),
    cooldown_until    TIMESTAMPTZ NOT NULL,
    reason            VARCHAR(50),               -- stop_hit, consecutive_losses, manual
    PRIMARY KEY (underlying_id, cooldown_until)
);
```

## New Module Structure

```
src/
├── fno/                              # New top-level module
│   ├── __init__.py
│   ├── calendar.py                   # NSE/BSE expiry resolution, holiday-aware
│   ├── ban_list.py                   # Daily F&O ban list collector + checker
│   ├── universe.py                   # Phase 1 universe filter
│   ├── catalyst_scorer.py            # Phase 2 catalyst scoring
│   ├── thesis_synthesizer.py         # Phase 3 LLM-based thesis generation
│   ├── intraday_manager.py           # Phase 4 active management loop
│   ├── strike_ranker.py              # 6-dimension scoring, returns top-N legs
│   ├── chain_collector.py            # Options chain fetcher (Angel One)
│   ├── chain_parser.py               # Compute IV, Greeks, PCR, max pain
│   ├── vix_collector.py              # India VIX fetcher + regime classifier
│   ├── iv_history_builder.py         # Daily IV percentile builder
│   ├── strategies/                   # Strategy abstraction
│   │   ├── __init__.py
│   │   ├── base.py                   # BaseStrategy interface
│   │   ├── long_call.py
│   │   ├── long_put.py
│   │   ├── debit_call_spread.py
│   │   ├── debit_put_spread.py
│   │   ├── credit_call_spread.py
│   │   └── credit_put_spread.py
│   ├── execution/                    # Slippage + paper-fill modeling
│   │   ├── __init__.py
│   │   ├── fill_simulator.py         # Realistic fill price from chain snapshot
│   │   └── sizer.py                  # Vol-scaled position sizing
│   ├── prompts.py                    # F&O-specific LLM prompts (thesis synthesis)
│   ├── notifications.py              # Telegram message formats for F&O
│   └── orchestrator.py               # Wires phases 1-4 into scheduler-callable funcs
└── api/routes/
    └── fno.py                        # GET endpoints for candidates, signals, theses
```

## Configuration Additions

Add to `src/config.py` as a `FNOSettings` Pydantic model loaded from `.env` and
overridable per environment. **Every threshold in this section must be a parameter,
not a constant in code.**

```
# F&O — Phase 1 (Universe filter)
FNO_PHASE1_MIN_ATM_OI=50000              # min OI at ATM strike
FNO_PHASE1_MAX_ATM_SPREAD_PCT=0.005      # 0.5% max bid-ask
FNO_PHASE1_MIN_AVG_VOLUME_5D=10000       # min 5-day avg option volume
FNO_PHASE1_MAX_DAYS_TO_EXPIRY=3          # only contracts expiring within 3 trading days
FNO_PHASE1_TARGET_OUTPUT=50              # narrow to top 50

# F&O — Phase 2 (Catalyst scoring)
FNO_PHASE2_NEWS_LOOKBACK_HOURS=18
FNO_PHASE2_MIN_COMPOSITE_SCORE=10        # below this → drop
FNO_PHASE2_TARGET_OUTPUT=20
FNO_PHASE2_WEIGHT_NEWS=1.0
FNO_PHASE2_WEIGHT_SENTIMENT=1.0
FNO_PHASE2_WEIGHT_FII_DII=0.8
FNO_PHASE2_WEIGHT_MACRO=0.8
FNO_PHASE2_WEIGHT_CONVERGENCE=1.5

# F&O — Phase 3 (Thesis synthesis)
FNO_PHASE3_TARGET_OUTPUT=10              # final candidate count
FNO_PHASE3_LLM_MODEL=claude-sonnet-4-20250514
FNO_PHASE3_LLM_TEMPERATURE=0.0           # deterministic for replay

# F&O — Phase 4 (Intraday management)
FNO_PHASE4_NO_ENTRY_BEFORE_MINUTES=30    # 30-min rule (overridable per strategy)
FNO_PHASE4_HARD_EXIT_TIME=14:30          # 2:30 PM IST
FNO_PHASE4_OI_RECHECK_INTERVAL_MIN=30
FNO_PHASE4_NEWS_RECHECK_INTERVAL_MIN=15
FNO_PHASE4_SCALE_OUT_AT_PCT_GAIN=0.30    # close 50% at +30% premium
FNO_PHASE4_TRAILING_STOP_FROM_PEAK_PCT=0.20
FNO_PHASE4_MAX_OPEN_POSITIONS=3
FNO_PHASE4_COOLDOWN_AFTER_STOP_MINUTES=120

# India VIX regime
FNO_VIX_LOW_THRESHOLD=12
FNO_VIX_HIGH_THRESHOLD=18
FNO_VIX_RECHECK_INTERVAL_MIN=5

# Position sizing (vol-scaled)
FNO_SIZING_RISK_PER_TRADE_PCT=0.01       # 1% portfolio risk per trade
FNO_SIZING_MAX_POSITION_PCT=0.15         # absolute cap (was 20% in original doc — reduced)
FNO_SIZING_USE_ATR_SCALING=true

# Strike ranker weights (also stored per-version in ranker_configs table)
FNO_RANKER_VERSION=v1
FNO_RANKER_W_DIRECTIONAL=0.30
FNO_RANKER_W_CONVERGENCE=0.20
FNO_RANKER_W_IV_VALUE=0.15
FNO_RANKER_W_THETA=0.10
FNO_RANKER_W_OI_STRUCTURE=0.15
FNO_RANKER_W_LIQUIDITY=0.10
```

## The Four-Phase Workflow

### Phase 1 — Structural Filter (~205 → 50)

* **When**: 6:30 AM IST daily (uses overnight data); also re-runnable at 7:30 AM
* **Inputs**: latest stored `options_chain` snapshot per instrument, `instruments`,
  today's `fno_ban_list`
* **Logic**:
  1. Pull all `instruments` where `is_fno = true`.
  2. Drop names in `fno_ban_list` for today.
  3. For each, check ATM strike from latest chain snapshot (find the strike closest to
     last `underlying_ltp`):
     * `oi >= FNO_PHASE1_MIN_ATM_OI`
     * `(ask - bid) / mid <= FNO_PHASE1_MAX_ATM_SPREAD_PCT`
  4. Compute 5-day average option volume from `options_chain`; require
     `>= FNO_PHASE1_MIN_AVG_VOLUME_5D`.
  5. Verify a contract exists with expiry within `FNO_PHASE1_MAX_DAYS_TO_EXPIRY`.
  6. Insert all surviving instruments into `fno_candidates` with `phase=1` and
     `passed_liquidity=true`.
  7. Cap at `FNO_PHASE1_TARGET_OUTPUT` ranked by ATM OI descending.
* **Output**: ~50 instruments stored as Phase 1 candidates with structural metadata.

### Phase 2 — Catalyst Scoring (50 → 20)

* **When**: continuously from 6 PM previous day through 8:30 AM, but the scoring
  consolidation runs at 8:30 AM IST.
* **Inputs**: Phase 1 candidates, recent `signals` and `raw_content`, FII/DII data,
  global macro RSS data (from new `macro_collector` — see Task 3 below).
* **Per-candidate scoring**:
  * **News score (0–5)**: count of unique articles in last 18h mentioning the
    instrument, log-scaled. SimHash dedup so PTI rewrites count once.
  * **Sentiment score (–5 to +5)**: weighted average of LLM-extracted sentiment
    across mentions. Reuses `extraction/llm_extractor.py` with the existing
    sentiment field.
  * **FII/DII score (0–5)**: did institutions show unusual activity (vs 30-day
    median) in the underlying yesterday? From new `fii_dii_collector`.
  * **Global macro alignment (0–5)**: does overnight macro (Brent, gold, copper,
    DXY, US futures) directionally support a move in this instrument's sector?
    Use `instruments.sector` + a sector→macro mapping table (see Task 4).
  * **Convergence score**: read from existing `signals.convergence_score` (Phase 3
    of base Laabh) for any active signals on the underlying.
* **Composite**:
  ```
  composite = (news * W_NEWS
             + |sentiment| * W_SENTIMENT  -- magnitude, direction kept separately
             + fii_dii * W_FII_DII
             + macro_align * W_MACRO
             + convergence * W_CONVERGENCE)
  ```
  Plus a directional polarity (sign of sentiment) stored alongside.
* **Output**: top `FNO_PHASE2_TARGET_OUTPUT` (20) by composite score, persisted as
  Phase 2 rows in `fno_candidates`. Drop anything below `FNO_PHASE2_MIN_COMPOSITE_SCORE`.

### Phase 3 — Technical Positioning + Thesis (20 → 10)

* **When**: 8:30–9:00 AM IST.
* **Inputs**: Phase 2 candidates, current `options_chain` snapshot, `iv_history`,
  India VIX regime.
* **Per-candidate technical pre-check** (deterministic, no LLM):
  * Distance from key levels: 52-week high/low, 20/50 SMA on `price_daily`.
  * IV regime: classify as `cheap` (< 30th percentile), `neutral` (30–70), or
    `expensive` (> 70) using `iv_history.iv_percentile_52w`.
  * OI structure read: identify highest call OI strike (resistance ceiling),
    highest put OI strike (support floor), max-pain strike.
  * OI delta read: classify last 24h OI movement at key strikes as
    `long_buildup`, `short_buildup`, `long_unwinding`, or `short_covering` based on
    price change × OI change at that strike.
* **LLM synthesis** (one call per candidate, batched 5 per request to reduce latency):
  * Inputs: candidate metadata, top 3 recent signals/news mentions, technical
    pre-check output, IV regime, OI structure, current VIX regime.
  * Output JSON: `{decision: "pass"|"skip", thesis: str, recommended_strategy: str,
    confidence: 0..1}` where `recommended_strategy` ∈ the 6 supported strategy types
    (must align with VIX regime: e.g. high VIX disallows naked long calls/puts).
  * Use `FNO_PHASE3_LLM_MODEL` and `temperature=0` for replayability. Store the
    full prompt and response in a separate `llm_audit_log` table (add as part of
    this phase) for replay.
* **Output**: top `FNO_PHASE3_TARGET_OUTPUT` (10) with `llm_decision='pass'`. These
  feed the strike ranker and become signal proposals.

### Phase 4 — Intraday Active Management

* **When**: continuously from 9:15 AM to 14:30 IST.
* **Sub-loops**:
  1. **Entry loop** (every 1 min after 9:45 AM until 13:00):
     * For each Phase 3 candidate without an active `fno_signal`, run the strike
       ranker. If top-ranked option score ≥ `FNO_RANKER_MIN_ENTRY_SCORE`, propose
       an `fno_signal` and paper-fill it via the fill simulator.
     * Respect `FNO_PHASE4_NO_ENTRY_BEFORE_MINUTES` per-strategy override (some
       strategies may permit earlier entry; default = 30 min).
     * Respect `FNO_PHASE4_MAX_OPEN_POSITIONS`.
     * Respect `fno_cooldowns`.
  2. **Position management loop** (every 1 min):
     * For each `active` `fno_signal`, recompute net premium from latest chain.
     * If `current_premium >= scale_out_threshold` and not yet scaled out:
       close 50%, transition to `scaled_out_50`, set trailing peak.
     * If trailing stop from peak triggered → close, transition to `closed_stop`.
     * If `current_premium <= stop_premium_net` → close, transition to `closed_stop`,
       create cooldown row.
     * If `current_premium >= target_premium_net` → close, transition to `closed_target`.
  3. **OI re-check loop** (every 30 min):
     * For each open position, re-read OI at the relevant strike. If buildup-type
       has flipped against the position (e.g. long position but strike now showing
       `long_unwinding`), emit a "level breaking" notification but do not auto-exit.
  4. **News re-check loop** (every 15 min):
     * For each open position, scan new signals on the underlying. If a contradicting
       high-confidence signal appears (`convergence_score >= 4` opposite direction),
       emit a "thesis-conflict" notification.
  5. **Hard time exit** (cron at `FNO_PHASE4_HARD_EXIT_TIME`):
     * Close ALL open `fno_signals` regardless of P&L. Transition to `closed_time`.

## Strike Ranker — Six Dimensions

Implement in `src/fno/strike_ranker.py`. Pure function: given the current chain
snapshot, candidate metadata, VIX regime, and the directional thesis, return a
ranked list of `(strategy_type, legs, score, breakdown)` tuples.

| Dimension | What it measures | How |
|---|---|---|
| Directional confidence | How strongly the thesis points one way | Sentiment magnitude + convergence + analyst credibility from existing scoreboard |
| Convergence | Cross-source agreement on direction | Existing `signals.convergence_score` |
| IV value | Are options cheap or expensive? | `iv_percentile_52w`: < 30 favors buying, > 70 favors selling. **Veto**: in high VIX regime, naked long options score 0 here |
| Theta drag | Time decay risk over planned hold | Days-to-expiry bucket × strategy theta exposure. Same-day expiry naked options heavily penalized after 13:00 |
| OI structure | Does positioning support the trade? | Buildup type × distance from OI walls |
| Liquidity | Will we exit cleanly? | (bid-ask % spread) × (size / OI) — penalize illiquid far OTM |

Final score is a weighted sum using `FNO_RANKER_W_*` weights, normalized 0–100.
The breakdown JSONB stored in `fno_signals.ranker_breakdown` records each dimension's
raw and weighted contribution so weight changes can be replayed offline.

## Strategy Abstraction

`src/fno/strategies/base.py`:

```
class BaseStrategy:
    name: str                                  # "long_call", "debit_call_spread", etc.

    def is_eligible(self, vix_regime: str, iv_regime: str,
                    days_to_expiry: int) -> tuple[bool, str]:
        """Return (eligible, reason). E.g. credit spreads need IV percentile > 50."""

    def construct_legs(self, underlying_ltp: float, chain: ChainSnapshot,
                       direction: str, sizing_input: SizingInput) -> list[Leg]:
        """Build the legs (strike, type, action, qty_lots)."""

    def compute_economics(self, legs: list[Leg], chain: ChainSnapshot) -> Economics:
        """Net premium, max loss, max profit, breakeven."""

    def default_targets(self, economics: Economics) -> tuple[float, float]:
        """Return (target_net_premium, stop_net_premium) for this strategy."""
```

Concrete strategies: `long_call`, `long_put`, `debit_call_spread`, `debit_put_spread`,
`credit_call_spread`, `credit_put_spread`. Adding a new strategy = one file, one
registry entry — no other code changes.

VIX-regime gating examples (encoded in `is_eligible`):
* `long_call` / `long_put`: NOT eligible when VIX > 18 (use spreads instead)
* `credit_*_spread`: NOT eligible when IV percentile < 50 (no premium to sell)
* `debit_*_spread`: eligible in any regime — preferred when VIX > 18

## Fill Simulator + Slippage

`src/fno/execution/fill_simulator.py`:

* Entry fill price: `mid + (ask - bid) * 0.5 * f(size/OI)` where `f` ramps from 0
  to 0.5 as `size/OI` goes 0 → 0.05. Caps at the ask.
* Exit fill price: same but symmetric.
* For multi-leg strategies, fill each leg independently with this rule.
* Modeled costs deducted from realized P&L:
  * Brokerage: `min(0.0003 * notional, ₹20)` per leg per side
  * STT on options: 0.05% on premium (sell side only)
  * SEBI turnover: 0.0001% on notional
  * Stamp duty: 0.003% on buy
  * GST: 18% on brokerage
* Use `Decimal` everywhere — never float — same as Phase 2 trading engine.

## Vol-Scaled Sizing

`src/fno/execution/sizer.py`:

* Compute `underlying_atr_pct = ATR(14) / current_price` from `price_daily`.
* `risk_budget = portfolio_value * FNO_SIZING_RISK_PER_TRADE_PCT`
* For defined-risk strategies: `lots = floor(risk_budget / max_loss_per_lot)`
* For naked options: `lots = floor(risk_budget / (premium_per_lot * stop_pct))`
* Clamp by `FNO_SIZING_MAX_POSITION_PCT * portfolio_value / notional_per_lot`.
* For high-`underlying_atr_pct` (top decile vs 90-day window), apply 0.5× damper.

## Notifications (Telegram)

### Morning brief (sent at 9:10 AM IST after Phase 3 completes)

```
🌅 F&O Morning Brief — 28 Apr 2026

VIX: 14.3 (neutral regime) | Nifty futures: +0.4%

Top 10 candidates with theses:
1. RELIANCE — IV cheap (P22) | Long call 1280 CE | conf: 0.78
   "Crude up 2% overnight + cross-source bullish convergence"
2. HDFCBANK — IV neutral | Debit call spread 1700/1750 | conf: 0.71
   "Rate-cut chatter, OI building at 1700 support"
...
F&O ban list today: 3 names — IRCTC, MANAPPURAM, RBLBANK
```

### Entry alert (when Phase 4 fills a paper trade)

```
🟢 F&O ENTRY — RELIANCE 1280 CE (1 May expiry)
Strategy: long_call | Lots: 2 | Entry: ₹42.50
Target: ₹56.00 (+32%) | Stop: ₹29.75 (-30%)
Ranker: 81/100 (directional 26 / iv_value 14 / oi 12 / liquidity 9 / theta 8 / convergence 12)
Open positions: 2/3 | VIX regime at entry: neutral
```

### Exit alert

```
🔴 F&O EXIT — RELIANCE 1280 CE
Reason: scale_out_50 hit | Closed 50% at ₹55.20 (+30%)
Trailing stop active on remaining: peak ₹55.20, trail to ₹44.16
```

### Critical alerts

* **Thesis conflict**: contradicting signal during open position
* **OI flip**: buildup-type reversed against position
* **Cooldown**: stop hit, instrument frozen for 2h
* **Hard exit**: 2:30 PM all positions closed

## Sequenced Implementation Tasks

Each task is sized to be a single Claude Code prompt. Run them in order. Each task
ends with self-verifying acceptance tests.

### Task 1 — Schema migration + models

* **Goal**: Add F&O DDL via Alembic migration, add SQLAlchemy ORM models.
* **Files**: `database/migrations/<timestamp>_add_fno_intelligence_module.py`,
  `src/models/fno_chain.py`, `src/models/fno_iv.py`, `src/models/fno_vix.py`,
  `src/models/fno_candidate.py`, `src/models/fno_signal.py`,
  `src/models/fno_ban.py`, `src/models/fno_ranker_config.py`,
  `src/models/fno_cooldown.py`.
* **Acceptance**:
  * `alembic upgrade head` runs cleanly on a fresh DB and on top of Phase 1–3 schema.
  * `pytest tests/test_fno_models.py` — instantiate each model, insert + query a row.
  * Hypertable check: `SELECT * FROM timescaledb_information.hypertables` shows
    `options_chain` and `vix_ticks`.
* **Rollback**: `alembic downgrade -1`.

### Task 2 — Calendar + ban list + VIX

* **Goal**: Get the three foundation pieces live before any chain work.
* **Files**: `src/fno/calendar.py`, `src/fno/ban_list.py`, `src/fno/vix_collector.py`,
  `tests/test_fno_calendar.py`, `tests/test_fno_ban_list.py`,
  `tests/test_fno_vix.py`.
* **Acceptance**:
  * `calendar.next_weekly_expiry("NIFTY", today())` returns next Tuesday (or prior
    trading day if Tuesday is a holiday).
  * `ban_list.fetch_today()` populates `fno_ban_list` and respects ON DUPLICATE.
  * `vix_collector.run_once()` writes to `vix_ticks` with correct regime classification.
  * Tests cover: holiday on expiry day, ban list with malformed CSV, VIX feed
    timeout/retry.
* **Rollback**: revert PR; data tables remain (idempotent inserts).

### Task 3 — Chain collector + parser

* **Goal**: Periodically pull options chain from Angel One and persist with computed
  Greeks if not provided.
* **Files**: `src/fno/chain_collector.py`, `src/fno/chain_parser.py`,
  `tests/test_fno_chain.py`, `scripts/test_chain_collector.py`.
* **Acceptance**:
  * `chain_collector.collect(instrument_id)` writes a row per (strike, type) for the
    nearest 2 expiries, with both pricing and IV/Greeks columns populated.
  * Greeks computation matches a known reference (use `py_vollib` Black-Scholes for
    cross-check) within 0.01 absolute on Delta.
  * `oi_change` correctly reflects diff vs previous snapshot (handle missing prior
    snapshot).
  * Backoff and retry on Angel One rate limits.
* **Rollback**: disable chain_collector job in scheduler; data persists.

### Task 4 — Macro + FII/DII + sector mapping

* **Goal**: Add data sources Phase 2 needs.
* **Files**: `src/collectors/macro_collector.py` (Yahoo Finance / investing.com RSS:
  Brent, WTI, gold, copper, DXY, US index futures), `src/collectors/fii_dii_collector.py`
  (NSE provisional data scraper), `database/seed_sectors.sql` (sector→macro mapping),
  `tests/test_macro.py`, `tests/test_fii_dii.py`.
* **Acceptance**:
  * `macro_collector.collect()` writes a normalized record per macro instrument every
    15 min during pre-market window (06:00–09:15 IST).
  * `fii_dii_collector.fetch_yesterday()` writes daily FII/DII net buy/sell with
    instrument-level breakdowns where available.
  * Sector→macro mapping seed loads cleanly: every `instruments.sector` value has at
    least one mapped macro driver.
* **Rollback**: disable jobs; tables persist.

### Task 5 — IV history builder

* **Goal**: Daily job that computes ATM IV percentile per instrument.
* **Files**: `src/fno/iv_history_builder.py`, `tests/test_iv_history.py`.
* **Acceptance**:
  * Job runs at 16:00 IST, writes `iv_history` row per F&O instrument with today's
    ATM IV plus 52-week percentile and rank.
  * Bootstrap mode: backfill last 60 days of `options_chain` snapshots into
    `iv_history` for percentile baseline.
* **Rollback**: drop today's rows; rerun.

### Task 6 — Phase 1 universe filter

* **Goal**: Implement the structural filter end-to-end.
* **Files**: `src/fno/universe.py`, `tests/test_fno_phase1.py`.
* **Acceptance**:
  * `universe.run(date=today())` reads chain snapshots, applies all five filters,
    writes `fno_candidates` rows with `phase=1`.
  * Test fixture: synthetic chain data → expected pass/fail matrix.
  * Idempotent: rerunning replaces today's Phase 1 rows for the same instruments.
* **Rollback**: delete `fno_candidates WHERE phase=1 AND run_date=...`.

### Task 7 — Phase 2 catalyst scorer

* **Goal**: Score Phase 1 candidates on news/sentiment/FII-DII/macro/convergence.
* **Files**: `src/fno/catalyst_scorer.py`, `tests/test_fno_phase2.py`.
* **Acceptance**:
  * `catalyst_scorer.run(date=today())` reads Phase 1 candidates and writes Phase 2
    rows with all five sub-scores plus composite.
  * Top 20 selection deterministic given identical inputs.
  * Test: stub `signals` table with known mentions → predictable composite scores.
* **Rollback**: delete `fno_candidates WHERE phase=2 AND run_date=...`.

### Task 8 — Phase 3 thesis synthesizer

* **Goal**: LLM-driven thesis generation with deterministic, replayable prompts.
* **Files**: `src/fno/thesis_synthesizer.py`, `src/fno/prompts.py`,
  `tests/test_fno_phase3.py`.
* **Acceptance**:
  * `thesis_synthesizer.run(date=today())` calls LLM in batches of 5, writes Phase 3
    rows in `fno_candidates` + a `llm_audit_log` row per LLM call (with
    `caller='fno.thesis'` and `caller_ref_id=fno_candidates.id`).
  * `llm_audit_log` table already exists from Phase 1 — do NOT re-create it.
  * Pinned model + temperature=0 + full prompt stored = byte-identical replay.
  * Output JSON validated against schema; malformed responses retried up to 3 times,
    then candidate marked `llm_decision='skip'` with `notes='llm_parse_failed'`.
* **Rollback**: delete Phase 3 rows; audit log preserved.

### Task 9 — Strategy abstraction + strike ranker

* **Goal**: Pluggable strategies + 6-dimension ranker.
* **Files**: `src/fno/strategies/*.py`, `src/fno/strike_ranker.py`,
  `tests/test_fno_strategies.py`, `tests/test_fno_ranker.py`.
* **Acceptance**:
  * Each strategy class has `is_eligible`, `construct_legs`, `compute_economics`,
    `default_targets` and is registered in `strategies/__init__.py:STRATEGY_REGISTRY`.
  * Ranker test: synthetic chain + thesis → ranker returns expected top-3 with
    expected score breakdown JSON.
  * Adding a new strategy file requires zero ranker changes (the ranker iterates
    the registry).
* **Rollback**: feature flag to disable a specific strategy via config.

### Task 10 — Fill simulator + sizer

* **Goal**: Realistic paper fill + vol-scaled position sizing.
* **Files**: `src/fno/execution/fill_simulator.py`, `src/fno/execution/sizer.py`,
  `tests/test_fno_fills.py`, `tests/test_fno_sizer.py`.
* **Acceptance**:
  * Fill simulator: known chain snapshot + size → expected fill price within
    rounding tolerance, all costs deducted correctly.
  * Sizer: portfolio value + ATR + max_loss → expected lots; respects max position cap.
  * Decimal arithmetic throughout (no float).
* **Rollback**: revert PR.

### Task 11 — Phase 4 intraday manager

* **Goal**: Wire the active management loops.
* **Files**: `src/fno/intraday_manager.py`, `tests/test_fno_phase4.py`.
* **Acceptance**:
  * Entry loop respects 30-min rule, max-positions cap, cooldowns.
  * Position management correctly transitions through states:
    `proposed → paper_filled → active → scaled_out_50 → closed_*`.
  * Hard 2:30 PM exit closes all open positions in a single transaction.
  * Cooldown row created on every stop-hit close.
* **Rollback**: scheduler flag to disable Phase 4 loops; existing positions remain
  closeable manually via API.

### Task 12 — F&O notifications

* **Goal**: Telegram message formats for the F&O lifecycle.
* **Files**: `src/fno/notifications.py`, `tests/test_fno_notifications.py`.
* **Acceptance**:
  * Morning brief assembled from Phase 3 rows; renders within 4096-char Telegram
    limit (truncates intelligently).
  * Entry / exit / critical alerts route via existing `notification_service.py`.
  * Idempotency: same event fires once even on scheduler retries.
* **Rollback**: revert PR.

### Task 13 — Orchestrator + scheduler integration

* **Goal**: Wire Phases 1–4 + supporting collectors into the existing APScheduler.
* **Files**: `src/fno/orchestrator.py`, edits to `src/scheduler.py`.
* **New jobs to register** (all with timezone Asia/Kolkata, exclude NSE holidays):
  | Job | Schedule | Function |
  |---|---|---|
  | `fno_chain_collect` | every 5 min, 09:00–15:30 | `chain_collector.collect_all` |
  | `fno_vix_collect` | every 5 min, 09:00–15:30 | `vix_collector.run_once` |
  | `fno_iv_history_build` | 16:00 daily | `iv_history_builder.run` |
  | `fno_ban_list_fetch` | 18:00 daily | `ban_list.fetch_today` |
  | `fno_phase1` | 06:30 daily | `universe.run` |
  | `fno_phase2` | 08:30 daily | `catalyst_scorer.run` |
  | `fno_phase3` | 08:55 daily | `thesis_synthesizer.run` |
  | `fno_morning_brief` | 09:10 daily | `notifications.send_morning_brief` |
  | `fno_phase4_entry_loop` | every 1 min, 09:45–13:00 | `intraday_manager.entry_tick` |
  | `fno_phase4_manage_loop` | every 1 min, 09:15–14:30 | `intraday_manager.manage_tick` |
  | `fno_phase4_oi_recheck` | every 30 min, 09:30–14:30 | `intraday_manager.oi_recheck` |
  | `fno_phase4_news_recheck` | every 15 min, 09:30–14:30 | `intraday_manager.news_recheck` |
  | `fno_phase4_hard_exit` | 14:30 daily | `intraday_manager.hard_exit_all` |
* **Acceptance**:
  * `python -m src.main` registers all jobs and they appear in `apscheduler.jobs` API.
  * Holiday calendar respected (jobs skip on NSE holidays via `system_config`
    holiday list).
  * Smoke test: run on a Saturday → Phase 1–4 jobs do not fire; data collectors
    that don't depend on market hours still work.
* **Rollback**: feature flag `FNO_MODULE_ENABLED=false` skips all F&O job registration.

### Task 14 — Read-only API endpoints

* **Goal**: Expose F&O data for the future mobile UI.
* **Files**: `src/api/routes/fno.py`, `src/api/schemas/fno.py`,
  `tests/test_fno_api.py`.
* **Endpoints**:
  * `GET /fno/candidates?date=YYYY-MM-DD&phase={1|2|3}` — paginated
  * `GET /fno/signals?status=active` — open positions
  * `GET /fno/signals/{id}` — single signal with `ranker_breakdown` and `legs`
  * `GET /fno/morning-brief?date=YYYY-MM-DD` — assembled brief
  * `GET /fno/vix?from=...&to=...` — VIX time series
  * `GET /fno/ban-list?date=YYYY-MM-DD`
* **Acceptance**:
  * OpenAPI schema generated; pydantic schemas validate.
  * `pytest tests/test_fno_api.py` covers happy path + 404 + auth boundary.
  * Rate-limited at 60 rpm (consistent with Phase 2 API).
* **Rollback**: remove route registration.

### Task 15 — End-to-end smoke run + observability

* **Goal**: Prove the pipeline works for one full trading day.
* **Files**: `scripts/fno_smoke_run.py`, `docs/fno_runbook.md`.
* **Acceptance**:
  * Run on a live trading day with `FNO_MODULE_ENABLED=true`.
  * Within the day, verify in DB:
    1. ≥1 row in `fno_ban_list` for today.
    2. ≥1 VIX tick per 5 min during market hours.
    3. ≥1 chain snapshot per 5 min for at least the index instruments.
    4. Phase 1 produces ≥30 candidates.
    5. Phase 2 produces top-20 with non-null composite scores.
    6. Phase 3 produces top-10 with theses + LLM audit log entries.
    7. ≥1 Telegram message fires (at minimum the morning brief).
    8. `fno_phase4_hard_exit` closes any open positions at 14:30.
  * `docs/fno_runbook.md` covers: how to disable a phase, how to replay a day, how
    to inspect the LLM audit trail, how to bump a config version.
* **Rollback**: `FNO_MODULE_ENABLED=false` and proceed without F&O signals.

## Configuration Versioning

* `ranker_configs` table stores every ranker weight set used in production.
* On any change to `FNO_RANKER_W_*`, the orchestrator bumps `FNO_RANKER_VERSION` and
  inserts a new row.
* Every `fno_signals` row stamps the `ranker_version` it was generated under.
* This enables future regression: "Would this trade have fired under v1 vs v2?"
  without retraining anything in this POC.

## Observability — Minimum Bar for POC

* Every collector job logs to `job_log` (existing table) with `items_processed`,
  `duration_ms`, `status`, `error`.
* Each phase emits a structured run summary log line at completion:
  `phase=2 run_date=2026-04-28 inputs=50 outputs=20 dropped_below_threshold=8 duration_ms=4321`.
* Telegram alert fires if any of: chain freshness > 10 min stale during market hours,
  Phase 1/2/3 fail to complete by 9:00 AM, Phase 4 entry loop fails 3 consecutive ticks.
* Existing `notification_service` is reused; no new alerting infra.

## Testing Strategy

* **Unit**: each Task above ships its own test module. CI must pass before merge.
* **Integration**: `tests/integration/test_fno_pipeline.py` runs Phases 1→3 against
  a fixture DB seeded with one trading day of synthetic chain + signals data, and
  asserts Phase 3 produces an expected top-10 list.
* **Smoke**: `scripts/fno_smoke_run.py` runs against a live DB during market hours
  and validates the day-end assertions in Task 15.
* **Determinism**: Phase 3's LLM call uses temperature=0 and a pinned model. The
  same Phase 2 inputs MUST produce the same Phase 3 output. CI replays the audit
  log on every commit and asserts no drift in deterministic stages.

## What Success Looks Like

After this phase, `python -m src.main` with `FNO_MODULE_ENABLED=true` should, on a
live trading day:

1. Build today's F&O ban list and VIX time series.
2. Pull options chains for ~205 underlyings every 5 min, from market open to close.
3. At 8:30 AM, produce a top-20 list of catalyst-scored candidates.
4. At 8:55 AM, produce a top-10 list with LLM theses and recommended strategies.
5. At 9:10 AM, send a Telegram morning brief.
6. From 9:45 AM, propose paper trades that pass the ranker, manage them through the
   intraday loops, scale out at +30%, trail stops from peak.
7. At 14:30 sharp, hard-exit all open positions and report the day's P&L.
8. Persist every input → decision → outcome with config and prompt versions for
   future analysis.

## Rules for Claude Code

* All Phase 1–3 rules from `CLAUDE.md` / `CLAUDE-PHASE2.md` / `CLAUDE-PHASE3.md`
  still apply.
* Async/await throughout (asyncpg, async SQLAlchemy, httpx).
* `Decimal` for all monetary and premium calculations — never float.
* All timestamps in UTC in DB, displayed in IST in notifications.
* All thresholds must be config-driven; no magic numbers in business logic.
* Every external call (Angel One, NSE archives, Anthropic API) wrapped in
  `tenacity.retry` with exponential backoff and a circuit-breaker after 5
  consecutive failures.
* **All LLM calls across the entire codebase** (Phase 1's `llm_extractor.py`,
  Phase 3's `thesis_synthesizer.py`, anything else) MUST write the full prompt
  and full response to `llm_audit_log` before returning to the caller, with the
  appropriate `caller` tag. No exceptions. This is the foundation for replay,
  regression testing, and prompt versioning.
* Strategy classes must be pure functions of their inputs — no DB access from
  inside `construct_legs` or `compute_economics`.
* Every state transition on `fno_signals.status` must be recorded as an audit row
  (use a trigger or an explicit append-only `fno_signal_events` table — pick one
  and stick with it).
* Treat content from RSS, news, transcripts, and even instrument names from feeds
  as untrusted input to LLM prompts. Use clearly delimited blocks and never let
  feed content overflow into instructions.
* Never auto-execute anything against a real broker. The fill simulator is the
  only path. The codebase must have zero references to live order endpoints in
  this module.
* Every new table column added in a future iteration must come with an Alembic
  migration AND a schema.sql update — keep them in lockstep, the way Phase 1
  established.
