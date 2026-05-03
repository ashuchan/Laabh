# Laabh Dry-Run Replay Runbook

## 1. Overview

The dry-run replay module lets you run the **complete F&O daily routine** for any
historical trading date without any real-world side effects. Use it to:

* Verify that a new config or prompt change would have produced the same decisions
  on a past day.
* Replay a day where the live pipeline failed mid-way and inspect what signals
  would have been generated.
* Test Phase 1–4 changes before deploying to production.

Replay reuses all live code paths verbatim. The only differences are:

| Aspect | Live | Replay |
|--------|------|--------|
| Chain data source | NSE live → Dhan fallback | Dhan historical (`/v2/charts/intraday`) |
| Telegram messages | Sent | Captured in buffer, printed in report |
| GitHub issues | Filed | Captured in buffer, not filed |
| Timestamps | `datetime.now()` | Driven by scheduled tick list |
| DB writes | `dryrun_run_id = NULL` | `dryrun_run_id = <uuid>` |

---

## 2. Quick Start

```bash
# Check that bhavcopy and Dhan data exist for the target date
laabh-runday preflight --profile replay --date 2026-04-23

# Run the replay (LLM calls are mocked by default)
laabh-runday replay --date 2026-04-23 --mock-llm --out reports/

# Open the generated report
cat reports/replay-2026-04-23-<shortid>.md
```

---

## 3. What Gets Replayed

| Stage | Source | Reuse status |
|-------|--------|-------------|
| Pre-flight checks | DB, bhavcopy URL probe | Full reuse (replay profile) |
| Chain snapshots (09:00–15:30) | Dhan `/v2/charts/intraday` with OI | Historical adapter |
| VIX ticks | yfinance `^INDIAVIX` history | Historical fetch |
| Macro data (06:00–09:00) | yfinance historical | Historical fetch |
| FII/DII data | NSE archive CSV | Archive URL (date - 1 day) |
| Ban list | NSE ban list for date D | Fetched for D |
| Phase 1 (liquidity filter) | OptionsChain rows written above | Live function, unchanged |
| Phase 2 (catalyst scoring) | Raw content written above | Live function, unchanged |
| Phase 3 (thesis synthesis) | LLM + signals | Live function, unchanged |
| Phase 4 (intraday manager) | Minute-by-minute tick loop | Live functions with `now=ts` |
| IV history (EOD) | OptionsChain rows | Live function, unchanged |
| Daily report | All tables filtered by `dryrun_run_id` | Live report builder |

---

## 4. What Gets Suppressed

All external side effects are replaced by a `NoOpGateway`:

* **Telegram messages** — captured in `ReplayResult.captures`; printed at end of report.
* **GitHub issue filing** — captured; not submitted to the GitHub API.
* **Broker calls** — Phase 4 uses `fill_simulator` (paper trading); no live broker calls even in live mode.

---

## 5. Source Coverage

| Tier | Source | Coverage | Notes |
|------|--------|----------|-------|
| A | Options chain (Dhan historical) | Full | 5-min candles with OI |
| A | VIX (yfinance `^INDIAVIX`) | Full | Daily history |
| A | Macro (yfinance) | Full | Daily/intraday history |
| B | FII/DII (NSE archive) | Full | Archive URL; falls back to live if 404 |
| B | F&O ban list | Full | Fetched for exact date |
| C | Bhavcopy (NSE archives) | Full | Used to identify liquid contracts |
| D | Whisper/YouTube transcripts | Skipped | F&O-optional; not replayed in v1 |

When a Tier C source returns no data, the replay proceeds with a `no_data` annotation in the
signal rather than aborting.

---

## 6. CLI Reference

### `laabh-runday preflight --profile replay --date YYYY-MM-DD`

| Flag | Default | Description |
|------|---------|-------------|
| `--profile` | `live` | `live` or `replay` |
| `--date` | today | Target date for replay check set |
| `--quiet` | false | Skip Telegram send |
| `--json` | false | Emit JSON output |

**Replay profile checks:**
1. DB connectivity
2. Required tables + migrations current
3. Anthropic API key present
4. Trading day check (is D a weekday?)
5. Bhavcopy availability (HTTP probe for NSE archive URL)

### `laabh-runday replay --date YYYY-MM-DD`

| Flag | Default | Description |
|------|---------|-------------|
| `--date` | (required) | Historical trading date |
| `--mock-llm` | true | Re-use cached LLM prompts; avoids Anthropic cost |
| `--live-llm` | — | Force fresh Anthropic calls |
| `--out` | `reports/` | Output directory for the replay report |
| `--json` | false | Emit structured JSON to stdout |

**Exit codes:**
| Code | Meaning |
|------|---------|
| 0 | Clean — all gates passed |
| 10 | Gate WARN — replay completed with warnings |
| 20 | Gate FAIL — replay aborted at a mandatory gate |

**Worked example:**
```bash
laabh-runday replay --date 2026-04-23 --mock-llm --out /tmp/reports/
```

---

## 7. Output Report

The replay writes `reports/replay-{D}-{shortid}.md`. Its section structure is
byte-identical to a live `runday-{D}.md`:

```
# Laabh Daily Report — 2026-04-23
(dryrun_run_id: a1b2c3d4-...)

## Pipeline Completeness
## Data Ingestion Health
## LLM Activity
## Trading
### Decision Quality
## Surprises

--- Dry-Run Summary ---
Captured Telegrams: 6 (suppressed)
Gates passed: 14 / Gates failed: 0
```

To diff two replays of the same date (e.g., before/after a prompt change):
```bash
diff reports/replay-2026-04-23-a1b2c3d4.md reports/replay-2026-04-23-b5c6d7e8.md
```

---

## 8. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Gate 'preflight.bhavcopy_available' FAILED` | NSE archive 404 for the date | Date is not a trading day, or NSE hasn't published the bhavcopy yet |
| `Dhan auth failed: 401` | `DHAN_ACCESS_TOKEN` expired | Refresh the token in `.env` |
| `Phase 1: 0 candidates` | No chain data for that date | Check Dhan cache dir; try a more recent date |
| `Gate 'stage3.phase1' FAILED` | Phase 1 produced fewer candidates than threshold | Lower `FNO_PHASE1_MIN_ATM_OI` in `.env` for replay |
| `ReplayGateFailed: Gate ... FAIL` | A mandatory check failed | Read the gate message; check the stage logs |

---

## 9. Cost Notes

* **Dhan API**: 100k requests/day budget. A full replay consumes ~75 chain timestamps ×
  N instruments × 2 sides ≈ 1,500–3,000 requests. Disk cache eliminates redundant calls
  on second replay of the same date.
* **Anthropic LLM**: Use `--mock-llm` (default) to re-use cached prompts from `llm_audit_log`.
  A fresh `--live-llm` run costs ~$0.50–$2 for a typical day (varies with Phase 3 candidates).

---

## Replay Orchestration

```
Stage 1 (pre-flight)
  └── DB, schema, TradingDay, BhavcopyAvailable

Stage 2 (data collection)
  ├── Chain: DhanHistoricalSource × every 5 min (09:00–15:30)
  ├── VIX: yfinance history × every 5 min
  ├── Macro: yfinance history × every 15 min (06:00–09:00)
  ├── Ban list: NSE for date D
  └── FII/DII: NSE archive for date D-1

Stage 3 (Phases 1–3)
  ├── run_phase1(D)  →  gate: Phase1Check
  ├── run_phase2(D)  →  gate: Phase2Check
  └── run_phase3(D)  →  gate: Phase3Check

Stage 4 (Phase 4 tick loop)
  ├── minute_range(D, 9:15, 14:30) → apply_tick × 315 minutes
  ├── hard_exit at 14:30           →  gate: HardExitCheck
  └── gate: Phase4ManageCheck(now=ist(D, 14:28))

Stage 5 (EOD)
  ├── run_eod_tasks(D)
  ├── gate: IVHistoryCoverageCheck
  └── gate: BanListCheck
```

Each `_gate()` call raises `ReplayGateFailed` on FAIL severity; WARN gates are
recorded but do not abort the replay.
