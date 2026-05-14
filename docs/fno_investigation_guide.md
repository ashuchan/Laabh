# FNO Pipeline Investigation Guide

This is a live context document for Claude Code. Use it to diagnose why Phase 3 produced
zero PROCEED candidates, why the quant algo did not trade, or any other FNO/quant
pipeline failure. Keep it updated whenever a new root cause or fix is discovered.

---

## Quick Orientation

### Pipeline flow (all times IST, Mon–Fri)

```
06:00  fno_tier_refresh       — classify instruments into Tier 1 / Tier 2
07:00  run_phase1             — liquidity filter (ATM OI + spread + volume)
07:15  run_phase2             — catalyst scoring (news / sentiment / FII-DII / macro)
07:30  run_phase3             — LLM thesis synthesis → PROCEED / SKIP / HEDGE
08:30  _fno_morning_brief     — Telegram brief of PROCEED candidates
09:00  chain snapshot         — collect option chains (Tier 1 every 5 min, Tier 2 every 15 min)
09:05  VIX + ban list refresh
09:18  quant_orchestrator     — bandit loop starts (when LAABH_INTRADAY_MODE=quant)
14:30  hard_exit              — force-close all open positions
15:40  fno_eod                — IV history builder + EOD summary
18:00  yahoo_eod + fno_fii_dii — EOD price data + FII/DII figures
```

### DB credentials

```
psql -U postgres -d laabh
Password: Ashu@007saxe
(set PGPASSWORD=Ashu@007saxe before psql calls)
```

---

## Step 1 — Establish Service State

### Are logs being written today?

```powershell
Get-ChildItem "C:\Users\ashus\OneDrive\Documents\Code\Laabh\logs" |
  Sort-Object LastWriteTime -Descending | Select-Object -First 5 |
  Format-Table FullName, LastWriteTime
```

> **Note:** Loguru writes to `stderr` only (configured in `src/main.py:27`).
> The `logs/YYYY-MM-DD/app.log` files contain **Angel One SmartAPI** errors only
> (Python `logging` module, not loguru). If there is no folder for today, the
> scheduler either hasn't started or crashed at boot.

### Is the service actually running?

Check whether the chain collector has fired today — it's the most frequent observable:

```sql
SELECT MAX(snapshot_at) AT TIME ZONE 'Asia/Kolkata' latest_chain_ist,
       COUNT(DISTINCT instrument_id) instruments
FROM options_chain
WHERE snapshot_at >= CURRENT_DATE;
```

- Result within the last 5 minutes → service is running.
- Result from yesterday → service not started today; premarket pipeline missed.
- No rows → chain collector has never run or DB is empty.

### Did the quant loop start today?

```sql
SELECT p.name, q.date, q.starting_nav, q.final_nav,
       q.trade_count, q.winning_trades,
       LEFT(q.universe::text, 120) universe_preview
FROM quant_day_state q
JOIN portfolios p ON p.id = q.portfolio_id
WHERE q.date >= CURRENT_DATE - 3
ORDER BY q.date DESC;
```

- A row for today → loop started (universe was non-empty).
- No row → loop never started: check portfolio existence and universe.

---

## Step 2 — Diagnose the FNO Phase Cascade

### Single query: phase counts for today

```sql
SELECT run_date, phase,
       llm_decision,
       COUNT(*)                           total,
       BOOL_OR(passed_liquidity)          any_passed,
       ROUND(AVG(composite_score), 2)     avg_score,
       ROUND(MIN(composite_score), 2)     min_score,
       ROUND(MAX(composite_score), 2)     max_score
FROM fno_candidates
WHERE run_date = CURRENT_DATE AND dryrun_run_id IS NULL
GROUP BY run_date, phase, llm_decision
ORDER BY phase, llm_decision;
```

**Interpret the output:**

| What you see | Root cause |
|---|---|
| No rows at all | Service did not start today |
| Phase 1 rows only | Service started late (missed 07:15 cron) OR Phase 2 crashed |
| Phase 1 + 2, no Phase 3 | Phase 2 all scored below `fno_phase2_min_composite_score` (5.5) OR Phase 2 exception |
| Phase 3 rows all SKIP | LLM REGIME GATE blocking (see Phase 3 section below) |
| Phase 3 rows, some PROCEED | Pipeline healthy — check morning brief delivery |

### History: last 7 days

```sql
SELECT run_date, phase, llm_decision, COUNT(*) total,
       ROUND(AVG(composite_score), 2) avg_score
FROM fno_candidates
WHERE run_date >= CURRENT_DATE - 7 AND dryrun_run_id IS NULL
GROUP BY run_date, phase, llm_decision
ORDER BY run_date DESC, phase, llm_decision;
```

---

## Step 3 — Phase 1 Deep-Dive

**Code:** `src/fno/universe.py` — `run_phase1()`, `_get_atm_chain_row()`

### How many instruments are in the universe?

```sql
SELECT COUNT(*) total,
       COUNT(*) FILTER (WHERE is_fno)    fno,
       COUNT(*) FILTER (WHERE is_active) active,
       COUNT(*) FILTER (WHERE is_fno AND is_active) active_fno
FROM instruments;
```

### What tier are they?

```sql
SELECT tier, COUNT(*) FROM fno_collection_tiers GROUP BY tier ORDER BY tier;
```

### What did Phase 1 actually filter today?

```sql
SELECT passed_liquidity, COUNT(*)
FROM fno_candidates
WHERE run_date = CURRENT_DATE AND phase = 1 AND dryrun_run_id IS NULL
GROUP BY passed_liquidity;
```

> Phase 1 only writes **passing** instruments to the DB. A count of 67 passed
> means 148 failed silently — inspect the chain data to see why.

### Simulate Phase 1 OI distribution for any date

```sql
WITH per_inst_latest AS (
  SELECT oc.instrument_id, i.symbol, oc.underlying_ltp,
         oc.strike_price, oc.option_type, oc.oi, oc.expiry_date
  FROM options_chain oc
  JOIN instruments i ON i.id = oc.instrument_id
  JOIN (
    SELECT instrument_id, MAX(snapshot_at) max_snap
    FROM options_chain
    -- For a specific runtime, add: WHERE snapshot_at <= '<07:00 IST as UTC>'
    GROUP BY instrument_id
  ) ls ON ls.instrument_id = oc.instrument_id AND ls.max_snap = oc.snapshot_at
  WHERE i.is_fno = true AND i.is_active = true
),
atm_oi AS (
  SELECT p.instrument_id, p.symbol, p.expiry_date,
         SUM(p.oi) FILTER (
           WHERE ABS(p.strike_price - p.underlying_ltp) = (
             SELECT MIN(ABS(p2.strike_price - p2.underlying_ltp))
             FROM per_inst_latest p2 WHERE p2.instrument_id = p.instrument_id
           )
         ) atm_oi_total,
         COALESCE(t.tier, 2) tier
  FROM per_inst_latest p
  LEFT JOIN fno_collection_tiers t ON t.instrument_id = p.instrument_id
  GROUP BY p.instrument_id, p.symbol, p.expiry_date, t.tier
)
SELECT tier,
  COUNT(*)                                              total,
  COUNT(*) FILTER (WHERE atm_oi_total < 1000)           below_1k,
  COUNT(*) FILTER (WHERE atm_oi_total BETWEEN 1000 AND 1999)  oi_1k_2k,
  COUNT(*) FILTER (WHERE atm_oi_total BETWEEN 2000 AND 4999)  oi_2k_5k,
  COUNT(*) FILTER (WHERE atm_oi_total >= 5000)          above_5k
FROM atm_oi
GROUP BY tier ORDER BY tier;
```

### Which expiry does each instrument use?

```sql
SELECT expiry_date, COUNT(DISTINCT instrument_id) instruments, SUM(oi) total_oi
FROM options_chain
WHERE snapshot_at = (SELECT MAX(snapshot_at) FROM options_chain WHERE snapshot_at >= CURRENT_DATE)
GROUP BY expiry_date ORDER BY expiry_date;
```

**Expected post-SEBI-reform (Sept 2025):**
- `NIFTY` → nearest **Tuesday** (weekly)
- `BANKNIFTY`, `FINNIFTY`, `MIDCPNIFTY` → last **Tuesday of the month** (monthly only)
- All stocks → last **Tuesday of the month**

If NIFTY is on a far monthly expiry instead of the nearest Tuesday, `next_weekly_expiry`
in `src/fno/calendar.py` may have a bug.

### Thresholds currently in effect

```python
# src/config.py
fno_phase1_min_atm_oi        = 2000   # Tier 1 (lowered 2026-05-12)
fno_phase1_min_atm_oi_tier2  = 1000   # Tier 2
fno_phase1_max_atm_spread_pct_tier1 = 0.02  # 2% Tier 1
fno_phase1_max_atm_spread_pct       = 0.05  # 5% Tier 2
fno_phase1_oi_collapse_pct          = 0.40  # 40% of rolling avg
fno_phase1_oi_collapse_min_days     = 3     # need 3 days history to fire
```

---

## Step 4 — Phase 2 Deep-Dive

**Code:** `src/fno/catalyst_scorer.py` — `run_phase2()`

Phase 2 reads Phase-1 passers and scores each on five dimensions. An instrument only gets
a row written when `composite_score >= fno_phase2_min_composite_score` (default **5.5**).

### Check the input data quality

```sql
-- Sentiment at Phase 2 runtime (~07:15 IST today)
SELECT fetched_at AT TIME ZONE 'Asia/Kolkata' fetched_ist,
       content_text::json->>'score' sentiment_score,
       content_text::json->'components'->'vix'->>'value' vix,
       content_text::json->'components'->'trend_1d'->>'nifty_pct' nifty_1d_pct
FROM raw_content
WHERE media_type = 'sentiment'
  AND fetched_at <= (CURRENT_DATE::timestamp + INTERVAL '7 hours 15 minutes')
               AT TIME ZONE 'Asia/Kolkata'
ORDER BY fetched_at DESC LIMIT 3;

-- Latest FII/DII data
SELECT fetched_at AT TIME ZONE 'Asia/Kolkata', content_text
FROM raw_content WHERE media_type = 'fii_dii'
ORDER BY fetched_at DESC LIMIT 2;

-- Signals in the 18h window at Phase 2 runtime
SELECT action, COUNT(*) signals, COUNT(DISTINCT instrument_id) instruments
FROM signals s
JOIN instruments i ON i.id = s.instrument_id
WHERE i.is_fno = true
  AND s.created_at >= (CURRENT_DATE::timestamp + INTERVAL '7 hours 15 minutes')
              AT TIME ZONE 'Asia/Kolkata' - INTERVAL '18 hours'
  AND s.created_at <  (CURRENT_DATE::timestamp + INTERVAL '7 hours 15 minutes')
              AT TIME ZONE 'Asia/Kolkata'
GROUP BY action;
```

### Composite score formula (understand why nothing passes)

```
composite = weighted avg of:
  news_score      × 1.0   (5=neutral, higher=bullish, lower=bearish)
  sentiment_score × 1.0
  fii_dii_score   × 0.8
  macro_score     × 0.8
  convergence     × 1.5   (agreement across all four)
```

When `sentiment_score = 3.87` (bearish, VIX high + Nifty -1.5%):
- Even a stock with neutral signals scores ~4.4 composite → fails 5.5 threshold
- Only stocks with very bullish news (score ~9+) can compensate → very few pass

**This is correct behavior** — Phase 2 correctly admits nothing in a bearish regime.

> **Design limitation:** composite ≥ 5.5 is bullish-biased and cannot identify bearish
> conviction plays (stocks with massive SELL signals that are good bear-spread candidates).
> See "Known Bugs" section for the proposed fix.

### When Phase 2 produced 0 rows despite signals existing

Check whether `run_phase2` was even called or crashed silently. Phase 2 is invoked
directly from `run_premarket_pipeline()` in `src/fno/orchestrator.py` with no
surrounding try/except — any exception at the top of `run_phase2` will abort the
whole pipeline and Phase 3 will never run.

Smoke-test Phase 2 input fetches directly:

```bash
python -c "
import asyncio
from src.db import session_scope
from src.fno.catalyst_scorer import get_latest_fii_dii, _get_latest_macro, _get_sentiment_score
async def check():
    async with session_scope() as s:
        fii, dii = await get_latest_fii_dii(s)
        macro = await _get_latest_macro(s)
        sent = await _get_sentiment_score(s)
        print(f'fii={fii} dii={dii} macro_keys={len(macro)} sentiment={sent}')
asyncio.run(check())
"
```

---

## Step 5 — Phase 3 Deep-Dive

**Code:** `src/fno/thesis_synthesizer.py` — `run_phase3()`

### Check the LLM audit log

```sql
SELECT created_at AT TIME ZONE 'Asia/Kolkata' time_ist,
       response_parsed->>'decision'              decision,
       (response_parsed->>'confidence')::numeric confidence,
       LEFT(response_parsed->>'thesis', 200)     thesis_preview
FROM llm_audit_log
WHERE caller = 'fno.thesis_synthesizer'
ORDER BY created_at DESC LIMIT 20;
```

### Zero rows in Phase 3 even though Phase 2 has passers

The most common cause: `underlying_ltp is None` for all Phase-2 candidates.
Phase 3 skips (with a logger.warning) before calling the LLM when it cannot find a valid
LTP. It will NOT write a row to `fno_candidates` in this case.

Check LTP sources:

```sql
-- Option A: OptionsChain (primary source)
SELECT i.symbol,
       MAX(oc.snapshot_at) AT TIME ZONE 'Asia/Kolkata' latest_snap,
       MAX(oc.underlying_ltp) latest_ltp
FROM options_chain oc
JOIN instruments i ON i.id = oc.instrument_id
WHERE i.is_fno = true
GROUP BY i.symbol
ORDER BY latest_snap DESC LIMIT 10;

-- Option B: PriceDaily (fallback)
SELECT i.symbol, MAX(pd.date) latest_date, MAX(pd.close) latest_close
FROM price_daily pd
JOIN instruments i ON i.id = pd.instrument_id
WHERE i.is_fno = true
GROUP BY i.symbol
ORDER BY latest_date DESC LIMIT 10;
```

If both are empty or stale for most instruments, Phase 3 will silently produce zero rows.

### ALL SKIP despite valid LTP

The v4 REGIME GATE (pre 2026-05-11) forced SKIP whenever `iv_regime = 'high'`.
After the iv_history fix on 2026-05-08 wired real IV ranks, most instruments showed
iv_rank 85–99% → all SKIPped.

**v5 prompt (deployed 2026-05-11 evening) fixes this.** In v5, high IV pivots to a
debit/credit spread structure — it no longer forces SKIP unless the structure itself
has unfavorable EV.

Verify the prompt version in use:

```python
# src/fno/prompts.py
FNO_THESIS_PROMPT_VERSION = "v5"  # should be v5 or later
```

Check IV rank data quality:

```sql
SELECT i.symbol,
       iv.date, iv.iv_rank_52w, iv.atm_iv
FROM fno_iv_history iv
JOIN instruments i ON i.id = iv.instrument_id
ORDER BY iv.date DESC, i.symbol
LIMIT 30;
```

Out-of-range iv_rank (e.g. -6000, +8100) is treated as missing and renders
`"unknown (no IV history)"` in the LLM prompt, which previously caused the REGIME GATE
to default to SKIP.

---

## Step 6 — Quant Universe and Loop

**Code:** `src/quant/orchestrator.py` — `run_loop()`
**Selector:** `src/quant/universe.py` — `HybridUniverseSelector`

The live quant loop uses `HybridUniverseSelector` which combines:
1. `TopGainersUniverseSelector` — deterministic base from `price_daily` (D-1 gainers/movers/gappers)
2. Phase-3 PROCEED supplements — LLM-identified catalysts added on top

If the universe is empty the loop aborts immediately:
```python
# src/quant/orchestrator.py:280
if not universe:
    logger.warning("[QUANT] Empty universe — aborting orchestrator loop")
    return
```

### Check the TopGainers data source

```sql
-- Does price_daily have recent FNO data?
SELECT COUNT(DISTINCT i.id) fno_instruments,
       MAX(pd.date)          latest_date,
       COUNT(DISTINCT i.id) FILTER (WHERE pd.date >= CURRENT_DATE - 7) recent_7d
FROM price_daily pd
JOIN instruments i ON i.id = pd.instrument_id
WHERE i.is_fno = true AND i.is_active = true;
```

If `latest_date` is more than 3 trading days ago, the TopGainers selector may return
empty (cannot compute prev_day_return without at least 2 rows per instrument).

`price_daily` is populated by `YahooFinanceCollector` which runs at 18:00 IST. It only
fetches instruments where `yahoo_symbol IS NOT NULL`. Check coverage:

```sql
SELECT COUNT(*) total, COUNT(yahoo_symbol) with_yahoo_symbol
FROM instruments WHERE is_fno = true AND is_active = true;
```

### Check the quant trades

```sql
SELECT p.name,
       qt.entry_at AT TIME ZONE 'Asia/Kolkata' entry_ist,
       qt.arm_id, qt.direction, qt.lots,
       qt.entry_premium_net, qt.exit_premium_net, qt.realized_pnl,
       qt.exit_reason, qt.status
FROM quant_trades qt
JOIN portfolios p ON p.id = qt.portfolio_id
WHERE qt.entry_at >= CURRENT_DATE
ORDER BY qt.entry_at DESC;
```

### Quant loop not starting even with non-empty universe

```sql
-- Check portfolio exists and is active
SELECT name, is_active, current_cash FROM portfolios WHERE name = 'Main Portfolio';
```

If no row or `is_active = false`, `_quant_orchestrator_loop` in `src/scheduler.py:305`
logs a warning and returns without starting.

---

## Step 7 — Job Log

```sql
-- All jobs last 3 days (excludes fno_premarket — it has no @_logged decorator)
SELECT job_name, status, created_at AT TIME ZONE 'Asia/Kolkata' created_ist,
       items_processed, signals_generated, error_message
FROM job_log
WHERE created_at >= CURRENT_DATE - 3 AND dryrun_run_id IS NULL
ORDER BY created_at DESC LIMIT 60;

-- Only key pipeline jobs
SELECT job_name, status, created_at AT TIME ZONE 'Asia/Kolkata' created_ist,
       items_processed, error_message
FROM job_log
WHERE created_at >= CURRENT_DATE - 7
  AND job_name IN ('yahoo_eod','fno_fii_dii','fno_eod','llm_extractor',
                   'fno_sentiment','rss_collector','google_news_collector')
  AND dryrun_run_id IS NULL
ORDER BY created_at DESC LIMIT 40;
```

> **Important:** `fno_premarket`, `_quant_orchestrator_loop`, and chain collection jobs
> (`fno_chain_tier1`, `fno_chain_tier2`) are NOT decorated with `@_logged` and will
> never appear in `job_log`. Use `quant_day_state` and `options_chain.snapshot_at`
> to confirm those ran.

### Yahoo EOD consistently 0 items

If `yahoo_eod` shows `items_processed = 0` every day, instruments lack `yahoo_symbol`.
Fix:

```sql
UPDATE instruments
SET yahoo_symbol = symbol || '.NS'
WHERE is_fno = true AND is_active = true AND yahoo_symbol IS NULL;
```

Then backfill manually:

```bash
python -c "
import asyncio
from src.collectors.yahoo_finance import YahooFinanceCollector
asyncio.run(YahooFinanceCollector(days=7).run())
"
```

---

## Step 8 — Raw Content and Signals

```sql
-- Recent raw content by type
SELECT media_type,
       COUNT(*) total,
       COUNT(*) FILTER (WHERE NOT is_processed) unprocessed,
       MAX(fetched_at) AT TIME ZONE 'Asia/Kolkata' latest_ist
FROM raw_content
WHERE fetched_at >= CURRENT_DATE - 2
GROUP BY media_type ORDER BY media_type;

-- Signal distribution for FNO instruments (last 3 days)
SELECT DATE(s.created_at AT TIME ZONE 'Asia/Kolkata') date_ist,
       s.action, COUNT(*) signals,
       COUNT(DISTINCT s.instrument_id) instruments
FROM signals s
JOIN instruments i ON i.id = s.instrument_id
WHERE i.is_fno = true AND s.created_at >= CURRENT_DATE - 3
GROUP BY date_ist, s.action
ORDER BY date_ist DESC, s.action;
```

---

---

## New Modules (Gaps 1-5, shipped 2026-05-13)

### Quick diagnostic queries

```sql
-- VRP snapshot for today
SELECT i.symbol, h.atm_iv, h.rv_20d*100 rv_pct, h.vrp*100 vrp_pts, h.vrp_regime
FROM iv_history h JOIN instruments i ON i.id = h.instrument_id
WHERE h.date = CURRENT_DATE AND h.vrp IS NOT NULL
ORDER BY ABS(h.vrp) DESC LIMIT 15;

-- Vol surface — skew and OI walls today
SELECT i.symbol, v.skew_regime, v.iv_skew_5pct, v.term_regime, v.pin_strike, v.pcr_near_expiry
FROM vol_surface_snapshot v JOIN instruments i ON i.id = v.instrument_id
WHERE v.run_date = CURRENT_DATE ORDER BY ABS(COALESCE(v.iv_skew_5pct,0)) DESC LIMIT 10;

-- Market regime today
SELECT regime, confidence, strategy_playbook, vix_current, nifty_1d_pct, vrp_median
FROM market_regime_snapshot WHERE run_date = CURRENT_DATE;

-- Portfolio Greeks (most recent)
SELECT logged_at AT TIME ZONE 'Asia/Kolkata', open_positions,
       net_delta, net_theta, net_vega, budget_warnings
FROM portfolio_greeks_log ORDER BY logged_at DESC LIMIT 5;

-- ML shadow: agreement rate last 30 days
SELECT ml_prediction, llm_decision, agreed, COUNT(*)
FROM ml_shadow_prediction WHERE run_date >= CURRENT_DATE - 30
GROUP BY ml_prediction, llm_decision, agreed ORDER BY agreed DESC;

-- ML shadow: win rate (once labeled data accumulates >=60 rows)
SELECT ml_prediction, ROUND(AVG(outcome_label::numeric)*100,1) win_pct, COUNT(*) n
FROM ml_shadow_prediction WHERE outcome_label IS NOT NULL GROUP BY ml_prediction;
```

---

## Codebase Reference Map

### FNO pipeline

| File | Purpose |
|---|---|
| `src/fno/orchestrator.py` | Entry point — `run_premarket_pipeline()` chains Phase 1→2→3 |
| `src/fno/universe.py` | Phase 1 liquidity filter — `run_phase1()`, `_get_atm_chain_row()` |
| `src/fno/calendar.py` | `next_weekly_expiry()` — resolves target expiry per symbol |
| `src/fno/catalyst_scorer.py` | Phase 2 scoring — `run_phase2()`, `score_*` helpers |
| `src/fno/thesis_synthesizer.py` | Phase 3 LLM synthesis — `run_phase3()` |
| `src/fno/prompts.py` | LLM system prompt + user template (versioned) |
| `src/fno/chain_collector.py` | NSE→Dhan option chain ingestion |
| `src/fno/entry_engine.py` | Contract proposals for Phase 3 PROCEED candidates |

### Quant loop

| File | Purpose |
|---|---|
| `src/quant/orchestrator.py` | Main 3-min bandit loop — `run_loop()` |
| `src/quant/universe.py` | `HybridUniverseSelector` (live) — TopGainers + Phase-3 PROCEED supplements |
| `src/quant/backtest/universe_top_gainers.py` | `TopGainersUniverseSelector` — price_daily based |
| `src/quant/circuit_breaker.py` | Kill-switch / lock-in / cooloff logic |
| `src/quant/context.py` | `OrchestratorContext` — injectable I/O bundle |
| `src/quant/primitives/` | ORB, VWAP revert, momentum, vol breakout strategies |

### Scheduler

| File | Purpose |
|---|---|
| `src/scheduler.py` | All APScheduler job registrations |
| `src/main.py` | Entry point — starts scheduler + Angel One WebSocket |

### Config and data

| File | Purpose |
|---|---|
| `src/config.py` | All `Settings` fields — Pydantic, loaded from `.env` |
| `src/models/fno_candidate.py` | `FNOCandidate` ORM model |
| `src/models/fno_chain.py` | `OptionsChain` ORM model |
| `src/models/price.py` | `PriceDaily` ORM model |
| `.env` | Active config overrides |

### Key DB tables

| Table | Written by | Contains |
|---|---|---|
| `fno_candidates` | Phase 1/2/3 | Pipeline results per instrument per day — only passing rows |
| `options_chain` | `chain_collector` | Option chain snapshots (OI, bid/ask, Greeks) |
| `price_daily` | `yahoo_eod` | EOD OHLCV per instrument |
| `llm_audit_log` | Phase 3 | Every Claude API call with prompt/response/tokens |
| `raw_content` | RSS, scrapers, collectors | Unprocessed and processed articles, FII/DII, macro, sentiment |
| `signals` | `llm_extractor` | Extracted BUY/SELL/HOLD signals per instrument |
| `quant_day_state` | `quant_orchestrator` | Session summary — universe, NAV, trade count |
| `quant_trades` | `quant_orchestrator` | Individual trade entries/exits |
| `job_log` | `@_logged` decorator | Collector run metadata (NOT FNO premarket or chain jobs) |
| `fno_iv_history` | `iv_history_builder` (15:40 IST EOD) | 52w IV rank, ATM IV per instrument |

---

## Known Bugs and Fixes Applied

### Fix 1 — Tier 1 OI threshold too high (2026-05-12)

**Symptom:** RELIANCE, BANKNIFTY, ICICIBANK, BAJAJ-AUTO, TITAN failing Phase 1 every day.

**Root cause:** `FNO_PHASE1_MIN_ATM_OI=5000` was calibrated against pre-SEBI-reform
weekly BANKNIFTY ATM OI (~100k). Post Sept-2025 reform, BANKNIFTY/FINNIFTY/MIDCPNIFTY
are monthly-only — monthly ATM OI is structurally 20–50× lower.

**Fix:** `.env` → `FNO_PHASE1_MIN_ATM_OI=2000`

---

### Fix 2 — Phase 1 used cross-expiry OI (2026-05-12)

**Symptom:** Phase 1 ATM OI included all expiries mixed together, inflating some figures
and using stale near-expiry OI for others.

**Root cause:** `_get_atm_chain_row()` had no expiry filter — it queried ALL chain rows
for the latest snapshot regardless of expiry date.

**Fix:** Added `expiry_date` parameter to `_get_atm_chain_row()`. `run_phase1()` now
computes `next_weekly_expiry(symbol, run_date)` per instrument and passes it through,
so Phase 1 measures OI on the exact contract that Phase 3 will trade.

**Code:** `src/fno/universe.py:133` (`_get_atm_chain_row`), `src/fno/universe.py:343`
(`run_phase1` — `target_expiry` computation)

---

### Fix 3 — No OI-collapse detection (2026-05-12)

**Symptom:** Instruments with anomalously collapsed OI (corporate events, circuit
breakers) passed Phase 1 because they cleared the static floor.

**Fix:** Added per-instrument rolling-average collapse guard in `run_phase1()`. A
batch query before the loop reads 10-day average ATM OI from `fno_candidates` history.
If `today_oi < 40% × rolling_avg`, the instrument fails with `oi_collapse:N<40%_of_10d_avg_M`.
New instruments (fewer than 3 days of history) are unconditionally admitted.

**Config:** `FNO_PHASE1_OI_COLLAPSE_PCT` (default 0.40), `FNO_PHASE1_OI_COLLAPSE_MIN_DAYS` (default 3)

---

### Fix 4 — v4 REGIME GATE too strict (2026-05-11)

**Symptom:** Phase 3 produced ALL SKIP from 2026-05-06 onward even when Phase 2 passed
candidates. LLM audit log showed every response citing "IV rank at 95–99% in high
regime, REGIME GATE prohibits naked longs."

**Root cause:** The iv_history wiring fix on 2026-05-08 replaced the hardcoded
`iv_rank=50` stub with real data. Real iv_ranks showed 85–99% for most instruments
(India VIX was elevated). The v4 system prompt's REGIME GATE said `→ SKIP` for any
high-IV candidate.

**Fix:** `src/fno/prompts.py` updated to v5. REGIME GATE now says: high-IV → **pivot
to debit/credit spread structure**, only SKIP when the structure itself has
unfavorable EV. `DECISION BIAS` section added to steer uncertain setups toward HEDGE
rather than SKIP.

**Note:** v5 only helps when Phase 2 passes candidates. On strongly bearish days
(sentiment < 4.5, net SELL signals in window), Phase 2 composite scores will be below
5.5 and Phase 3 will not run.

---

### Fix 5 — Phase 3 silently dropped all candidates (2026-05-08)

**Symptom:** Phase 2 had 9 passers, Phase 3 wrote zero rows (not even SKIP rows).

**Root cause:** `_get_underlying_ltp()` was introduced on 2026-05-08, replacing the
previous bug where `instrument.market_cap_cr` (always non-zero) was used as the
underlying price. After the fix, instruments with no OptionsChain data AND no
PriceDaily data returned `None` → skipped before the LLM call and before
`_upsert_phase3_candidate()` is reached.

**Fix:** OptionsChain data is now populated for all 214 instruments so
`_get_underlying_ltp()` resolves correctly. The silent skip still exists as a guard;
if it fires for an instrument it emits `logger.warning`.

---

## Known Design Limitation — FIXED 2026-05-13

### Phase 2 composite score bidirectional gate

Previously `composite_score >= 5.5` only passed bullish-leaning instruments. A stock
with heavy SELL signals (news_score = 2) got composite ≈ 3.5 and was rejected even
though it is an ideal bear-spread candidate.

**Fix implemented** in `src/fno/catalyst_scorer.py:637`:

```python
deviation = min_score - 5.0          # 0.5 when min_score=5.5
passed = abs(comp_s - 5.0) >= deviation
```

With `min_score = 5.5` this passes `composite >= 5.5` (bullish) **and** `composite <= 4.5`
(bearish). Phase 3 already handles `"direction": "bearish"` and recommends bear-put
spreads. The debug log now shows `PASS [BULLISH]` or `PASS [BEARISH]` to distinguish
direction at Phase 2 time.

**Triggered:** 2026-05-13 — sentiment=2.69 (VIX=19.28, Nifty -1.83%), FII net -₹8,437 Cr.
All 50 Phase 1 passers scored ~4.18 composite (below 5.5) and Phase 3 never ran.
After fix, bearish-conviction stocks (composite ≤ 4.5) now reach Phase 3.

Also fixed `_get_phase2_candidates` in `thesis_synthesizer.py` to order by
`abs(composite_score - 5.0) DESC` instead of `composite_score DESC`, so the
Phase 3 target-output cap always picks the most convicted candidates in either
direction (not the least-bearish ones on a down day).

---

## Debugging Tips

### Phase 2 produces 0 rows — quick arithmetic check

Plug in the day's inputs and compute the expected composite:

```python
from src.fno.catalyst_scorer import score_fii_dii, score_convergence, compute_composite

sentiment = 3.87   # from raw_content query
fii_net, dii_net = -4110.6, 6748.13  # from fii_dii query
fii_dii = score_fii_dii(fii_net, dii_net)  # expect 5.0 when mixed
news = 5.0         # if no signals

conv = score_convergence(news, sentiment, fii_dii, 5.0)
comp = compute_composite(news, sentiment, fii_dii, 5.0, conv)
print(f"fii_dii={fii_dii:.2f}  conv={conv:.2f}  composite={comp:.2f}")
# If composite < 5.5 → correct behavior, market is genuinely bearish
```

### Run Phase 1 manually (out of schedule)

```bash
python -c "
import asyncio
from datetime import date
from src.fno.universe import run_phase1
async def test():
    results = await run_phase1(date.today())
    passed = [r for r in results if r.passed]
    print(f'Passed {len(passed)}/{len(results)}')
    for r in results[:5]:
        print(r)
asyncio.run(test())
"
```

### Run the full premarket pipeline manually

```bash
python -c "
import asyncio
from src.fno.orchestrator import run_premarket_pipeline
asyncio.run(run_premarket_pipeline())
" 2>&1 | head -100
```

### Check if a specific instrument will pass Phase 1

```sql
WITH snap AS (
  SELECT MAX(snapshot_at) max_snap
  FROM options_chain oc
  JOIN instruments i ON i.id = oc.instrument_id
  WHERE i.symbol = 'RELIANCE'
    AND oc.expiry_date = (
      SELECT MIN(expiry_date) FROM options_chain oc2
      JOIN instruments i2 ON i2.id = oc2.instrument_id
      WHERE i2.symbol = 'RELIANCE' AND expiry_date >= CURRENT_DATE
    )
)
SELECT oc.strike_price, oc.option_type, oc.oi, oc.bid_price, oc.ask_price, oc.underlying_ltp
FROM options_chain oc
JOIN instruments i ON i.id = oc.instrument_id
JOIN snap ON oc.snapshot_at = snap.max_snap
WHERE i.symbol = 'RELIANCE'
ORDER BY ABS(oc.strike_price - oc.underlying_ltp), oc.option_type
LIMIT 6;
```

### Verify the v5 prompt is live

```bash
python -c "from src.fno.prompts import FNO_THESIS_PROMPT_VERSION; print(FNO_THESIS_PROMPT_VERSION)"
# Expected: v5
```

### Check the OI collapse rolling averages built so far

```sql
SELECT i.symbol,
       COUNT(fc.atm_oi) days_of_history,
       ROUND(AVG(fc.atm_oi)) rolling_avg_oi
FROM fno_candidates fc
JOIN instruments i ON i.id = fc.instrument_id
WHERE fc.phase = 1
  AND fc.run_date >= CURRENT_DATE - 14
  AND fc.atm_oi IS NOT NULL
  AND fc.dryrun_run_id IS NULL
GROUP BY i.symbol
HAVING COUNT(fc.atm_oi) >= 3
ORDER BY rolling_avg_oi DESC
LIMIT 20;
```

### Confirm the quant universe non-empty before market open

```bash
python -c "
import asyncio
from datetime import date
from src.quant.universe import HybridUniverseSelector
async def test():
    universe = await HybridUniverseSelector().select(date.today())
    print(f'{len(universe)} instruments in universe')
    for u in universe[:5]:
        print(' ', u['symbol'])
asyncio.run(test())
"
```

---

## Environment Variables Quick Reference

```bash
# In .env — key FNO and quant knobs
FNO_MODULE_ENABLED=true
FNO_PHASE1_MIN_ATM_OI=2000                # Tier 1 OI floor (lowered 2026-05-12)
FNO_PHASE1_MIN_ATM_OI_TIER2=1000
FNO_PHASE1_OI_COLLAPSE_PCT=0.40           # 40% collapse guard
FNO_PHASE1_OI_COLLAPSE_MIN_DAYS=3
FNO_PHASE2_MIN_COMPOSITE_SCORE=5.5        # passage threshold
FNO_PHASE2_NEWS_LOOKBACK_HOURS=18
FNO_PHASE3_TARGET_OUTPUT=30               # max candidates sent to LLM
LAABH_INTRADAY_MODE=quant                 # "quant" or "agentic"
LAABH_QUANT_PORTFOLIO_NAME=Main Portfolio
```
