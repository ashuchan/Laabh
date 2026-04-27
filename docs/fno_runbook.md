# F&O Intelligence Module — Runbook

## Overview

The F&O module is a multi-phase pipeline that identifies, scores, and paper-trades
options opportunities on NSE/BSE using a combination of quantitative filters, news
catalyst scoring, and LLM-based thesis synthesis.

**Module toggle**: set `FNO_MODULE_ENABLED=true` in `.env` to activate.

---

## Pipeline Phases

### Phase 1 — Liquidity Filter (07:00 IST, pre-market)

**Code**: `src/fno/universe.py` → `run_phase1()`

Screens every `is_fno=true` instrument in the database:

| Criterion | Default | Config key |
|-----------|---------|-----------|
| ATM OI ≥ N contracts | 50 000 | `FNO_PHASE1_MIN_ATM_OI` |
| ATM bid-ask spread ≤ X% | 0.5% | `FNO_PHASE1_MAX_ATM_SPREAD_PCT` |
| 5-day avg volume ≥ N | 10 000 | `FNO_PHASE1_MIN_AVG_VOLUME_5D` |

Instruments on the NSE F&O ban list are automatically excluded.

**Output**: `fno_candidates` rows with `phase=1, passed_liquidity=true/false`.

---

### Phase 2 — Catalyst Scoring (07:15 IST, after Phase 1)

**Code**: `src/fno/catalyst_scorer.py` → `run_phase2()`

Scores five catalyst dimensions for Phase-1 passing instruments:

| Dimension | Weight (default) | Source |
|-----------|-----------------|--------|
| News score | 1.0 | `signals` table (last 18h) |
| Sentiment | 1.0 | `raw_content` (media_type=sentiment) |
| FII/DII activity | 0.8 | `raw_content` (media_type=fii_dii) |
| Macro alignment | 0.8 | `raw_content` (media_type=macro) |
| Convergence | 1.5 | Derived from above |

Instruments with `composite_score ≥ FNO_PHASE2_MIN_COMPOSITE_SCORE` (default 10.0)
proceed to Phase 3.

**Output**: `fno_candidates` rows with `phase=2`.

---

### Phase 3 — Thesis Synthesis (07:30 IST, after Phase 2)

**Code**: `src/fno/thesis_synthesizer.py` → `run_phase3()`

Calls Claude API for each top Phase-2 candidate to produce:
- `decision`: PROCEED | SKIP | HEDGE
- `direction`: bullish | bearish | neutral
- `thesis`: reasoning paragraph
- `confidence`: 0.0–1.0

Every LLM call is logged to `llm_audit_log` for cost tracking and debugging.

**Config**:
```
FNO_PHASE3_LLM_MODEL=claude-sonnet-4-20250514
FNO_PHASE3_LLM_TEMPERATURE=0.0
FNO_PHASE3_TARGET_OUTPUT=10
```

**Output**: `fno_candidates` rows with `phase=3, llm_decision=PROCEED/SKIP/HEDGE`.

---

### Phase 4 — Intraday Management (09:15–15:30 IST)

**Code**: `src/fno/intraday_manager.py`, `src/fno/orchestrator.py`

Real-time lifecycle for paper positions:

| Gate | Value | Config key |
|------|-------|-----------|
| No entry before | 09:45 IST (+30 min) | `FNO_PHASE4_NO_ENTRY_BEFORE_MINUTES` |
| Hard exit | 14:30 IST | `FNO_PHASE4_HARD_EXIT_TIME` |
| Max open positions | 3 | `FNO_PHASE4_MAX_OPEN_POSITIONS` |
| Scale-out at gain | 30% | `FNO_PHASE4_SCALE_OUT_AT_PCT_GAIN` |
| Trailing stop from peak | 20% | `FNO_PHASE4_TRAILING_STOP_FROM_PEAK_PCT` |
| Cooldown after stop | 120 min | `FNO_PHASE4_COOLDOWN_AFTER_STOP_MINUTES` |

---

## Strategy Selection

**Code**: `src/fno/strategies/`, `src/fno/strike_ranker.py`

Six strategies are available; the ranker picks the best for the current regime:

| Strategy | Direction | IV Regime |
|----------|-----------|-----------|
| Long Call | Bullish | Low/Neutral |
| Long Put | Bearish | Low/Neutral |
| Bull Call Spread | Bullish | Any |
| Bear Put Spread | Bearish | Any |
| Iron Condor | Neutral | High |
| Straddle | Any | Low |

Ranker weights (config):
```
FNO_RANKER_W_DIRECTIONAL=0.30
FNO_RANKER_W_CONVERGENCE=0.20
FNO_RANKER_W_IV_VALUE=0.15
FNO_RANKER_W_THETA=0.10
FNO_RANKER_W_OI_STRUCTURE=0.15
FNO_RANKER_W_LIQUIDITY=0.10
```

---

## Position Sizing

**Code**: `src/fno/execution/sizer.py`

```
risk_budget = capital × FNO_SIZING_RISK_PER_TRADE_PCT   (default 1%)
lots = floor(risk_budget / max_risk_per_lot)
lots = min(lots, capital × FNO_SIZING_MAX_POSITION_PCT / premium_per_lot)
if vix_regime == "high": lots = lots // 2
```

---

## India VIX Regime

**Code**: `src/fno/vix_collector.py`

| VIX Level | Regime | Effect |
|-----------|--------|--------|
| < 12 | `low` | Prefer long premium strategies |
| 12–18 | `neutral` | No adjustment |
| > 18 | `high` | Prefer spreads/condors; halve position size |

Config: `FNO_VIX_LOW_THRESHOLD=12`, `FNO_VIX_HIGH_THRESHOLD=18`

---

## Scheduler Jobs

All jobs run on market days (Mon–Fri IST):

| Time | Job | Code |
|------|-----|------|
| 06:00–09:00, every 15 min | Macro data (yfinance) | `macro_collector.collect()` |
| 07:00 | Pre-market pipeline (Ph1+2+3) | `orchestrator.run_premarket_pipeline()` |
| 09:00 | Chain snapshot refresh | `chain_collector.collect_all()` |
| 09:05 | VIX + ban list refresh | `orchestrator.run_vix_refresh()` |
| 09:15–15:30, every 30 min | Intraday chain refresh | `chain_collector.collect_all()` |
| 09:15–15:30, every 5 min | VIX recheck | `vix_collector.run_once()` |
| 15:40 | EOD IV history + summary | `orchestrator.run_eod_tasks()` |
| 18:00 | FII/DII data | `fii_dii_collector.fetch_yesterday()` |

---

## Data Flow

```
yfinance (macro)    NSE FII/DII API    RSS + Google News
       │                  │                    │
       ▼                  ▼                    ▼
  raw_content         raw_content          raw_content
  (media=macro)    (media=fii_dii)     (media=news/article)
       │                  │                    │
       └──────────────────┴────────────────────┘
                          │
                    Phase 2 Catalyst Scorer
                          │
                    Phase 3 LLM Thesis (Claude)
                          │
                 fno_candidates (phase=3, PROCEED)
                          │
              Strategy selection + strike ranking
                          │
                   Paper fill simulation
                          │
                  intraday_manager (Phase 4)
                          │
              fno_signals + fno_signal_events
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/fno/candidates` | List pipeline candidates |
| GET | `/fno/candidates/{id}` | Get single candidate |
| GET | `/fno/iv-history/{instrument_id}` | IV history (52 weeks) |
| GET | `/fno/vix` | Recent VIX readings |
| GET | `/fno/ban-list` | F&O ban list |
| POST | `/fno/pipeline/trigger` | Manual pipeline trigger |

---

## Smoke Test

Validate the full pipeline with mock data (no DB or API keys needed):

```bash
python scripts/fno_smoke_run.py
```

Expected output: 24 checks, all `✅`.

---

## Common Issues

**Module not running**: Check `FNO_MODULE_ENABLED=true` in `.env`.

**Phase 3 LLM calls failing**: Verify `ANTHROPIC_API_KEY` is set and the model
`FNO_PHASE3_LLM_MODEL` is accessible.

**No Phase-1 candidates**: Ensure F&O instruments exist with `is_fno=true` and
that chain snapshots have been collected (`chain_collector.collect_all()`).

**VIX 0.0 or missing**: The Angel One token for India VIX is `26017`. Confirm
that the Angel One WebSocket is connected and the token is in the subscription list.

**F&O ban list fetch fails**: NSE archive URL format is
`fo_secban_{DDMMYYYY}.csv`. Check date formatting. Non-market days return 404
(handled gracefully — previous day's ban list remains active).
