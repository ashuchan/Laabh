# laabh-runday — Operator Runbook

`laabh-runday` is the live-day operations CLI for Laabh. It provides
deterministic, repeatable pre-flight checks, phase checkpoints, a live status
dashboard, and end-of-day reporting — all read-only against the operational
database.

---

## Installation

```bash
pip install -e .          # installs laabh-runday console script
laabh-runday --help       # verify installation
```

---

## Suggested Daily Flow

### Night Before (Monday evening)

```bash
laabh-runday preflight
```

Runs all connectivity, schema, and seed-data checks. Fix any FAILs before
sleeping. WARNs (e.g. ban list not yet fetched for tomorrow) are acceptable.

---

### Run Day

```
06:00  Start orchestrator
       FNO_MODULE_ENABLED=true python -m src.main

06:01  Quick preflight (Telegram suppressed)
       laabh-runday preflight --quiet

06:35  Verify tier refresh and Phase 1 completed
       laabh-runday checkpoint tier-refresh && laabh-runday checkpoint phase1

08:35  Verify Phase 2 completed
       laabh-runday checkpoint phase2

09:00  Verify Phase 3 completed
       laabh-runday checkpoint phase3

09:11  Verify morning brief was sent
       laabh-runday checkpoint morning-brief

09:15+ Open status dashboard in a tmux pane
       laabh-runday status --watch

14:31  Verify hard exit executed
       laabh-runday checkpoint hard-exit

16:05  Verify IV history collected
       laabh-runday checkpoint iv-history

18:01  Verify ban list fetched
       laabh-runday checkpoint ban-list

18:35  Verify review loop completed
       laabh-runday checkpoint review-loop
```

---

### Evening

```bash
laabh-runday report --markdown --telegram
```

Writes `reports/runday-YYYY-MM-DD.md` and sends an executive summary to Telegram.

---

## Subcommand Reference

### `preflight`

Pre-market sanity check. Run before each trading day.

```
laabh-runday preflight [--quiet] [--json] [--skip <check>]
```

| Flag | Description |
|------|-------------|
| `--quiet` | Skip the Telegram ping message |
| `--json` | Emit JSON instead of console table |
| `--skip <name>` | Skip a specific check by name (repeatable) |

**Exit codes:**
- `0` — all green
- `10` — warnings only (non-blocking)
- `20` — at least one failure (do not start trading)

**What it checks (in order):**
1. Required environment variables present
2. Database reachable (SELECT 1)
3. Alembic migrations current
4. Required F&O tables exist
5. Seed data (source_health rows, holiday calendar)
6. Anthropic API (1-token test call)
7. Telegram (sends `🟢 Laabh preflight at HH:MM IST`)
8. Angel One (login + tick fetch)
9. NSE (cookie warmup + option chain probe)
10. Dhan (POST /v2/optionchain)
11. GitHub (GET /repos/{repo}, rate-limit headroom)
12. Tier table populated (Tier 1 / Tier 2 counts)
13. Tomorrow is a trading day (not weekend/holiday)

---

### `checkpoint <phase>`

Phase-specific verification. Run after each phase's expected completion time.

```
laabh-runday checkpoint <phase> [--json] [--since YYYY-MM-DD] [--strict]
```

**Valid phases:**

| Phase | When to run | What it asserts |
|-------|------------|-----------------|
| `tier-refresh` | 06:35 | `fno_collection_tiers.updated_at` >= today 06:00 IST |
| `phase1` | 06:35 | ≥30 rows in `fno_candidates` for phase=1, today |
| `phase2` | 08:35 | exactly `FNO_PHASE2_TARGET_OUTPUT` rows, all scored |
| `phase3` | 09:00 | exactly `FNO_PHASE3_TARGET_OUTPUT` rows + llm_audit_log rows |
| `morning-brief` | 09:11 | notification sent today with is_pushed=true |
| `phase4-entry` | 09:15+ | entry loop ran ≥1 tick since 09:45 IST |
| `phase4-manage` | during market hours | manage loop ran in last 5 min |
| `hard-exit` | 14:31 | 0 active/filled/scaled positions |
| `iv-history` | 16:05 | ≥90% F&O instruments have IV row for today |
| `ban-list` | 18:01 | ban-list row(s) exist for today |
| `review-loop` | 18:35 | review loop ran; no unfiled GitHub issues |

**Flags:**

| Flag | Description |
|------|-------------|
| `--json` | Emit JSON output |
| `--since YYYY-MM-DD` | Override anchor date (replay yesterday's checks) |
| `--strict` | Treat warnings as failures |

---

### `status`

Live pipeline snapshot. Designed for `watch` use during market hours.

```
laabh-runday status [--json] [--once] [--watch]
```

| Flag | Description |
|------|-------------|
| `--json` | Dashboard as JSON |
| `--once` | Single snapshot and exit (default) |
| `--watch` | Refresh every 60s using rich.live |

**Dashboard sections:**
- Chain collection stats (last 10 min)
- Source health per data source
- Open chain collection issues
- Pipeline today (phases 1–3, morning brief, VIX, ban list)
- Trading today (positions, P&L)
- Recent job runs

---

### `tier-check`

Per-instrument chain coverage diagnostic.

```
laabh-runday tier-check [--filter degraded] [--tier {1|2}] [--limit N] [--json]
```

| Flag | Description |
|------|-------------|
| `--filter degraded` | Only show instruments with <80% success rate in last hour |
| `--tier 1` or `--tier 2` | Filter by tier |
| `--limit N` | Max rows (default: 50) |
| `--json` | Emit JSON output |

---

### `kill-switch`

Arm the F&O module kill-switch. Does NOT kill the process itself.

```
laabh-runday kill-switch [--reason <text>]
```

**What it does:**
1. Writes `FNO_MODULE_ENABLED=false` to `.env` atomically (tempfile + rename)
2. Finds and prints the orchestrator PID
3. Prints: `Now run: kill -TERM <pid>`
4. Sends Telegram alert: `🛑 F&O kill-switch armed by operator at HH:MM IST`

**Why two steps?** The operator must deliberately run `kill -TERM <pid>` — this
prevents fat-fingering.

---

### `report`

End-of-day rollup. Reads DB only, no external calls.

```
laabh-runday report [--date YYYY-MM-DD] [--json] [--markdown] [--telegram]
```

| Flag | Description |
|------|-------------|
| `--date` | Report date (default: today) |
| `--json` | Full structured JSON to stdout |
| `--markdown` | Write to `reports/runday-YYYY-MM-DD.md` |
| `--telegram` | Send executive summary to Telegram |

**Report sections:**
1. Pipeline completeness (which jobs ran / skipped)
2. Data ingestion health (chain stats, tier coverage, issues)
3. LLM activity (calls, tokens, cost estimate)
4. Trading layer (signals, P&L, decision quality table)
5. Surprises (anomalous conditions detected)

---

## Configuration

All thresholds are overridable via `.env` without code changes:

```bash
RUNDAY_MIN_PHASE1_CANDIDATES=30      # Phase 1 minimum row count
RUNDAY_MIN_CHAIN_NSE_SHARE_PCT=80.0  # NSE share minimum
RUNDAY_MAX_TIER1_LATENCY_MS_P95=3000 # Tier 1 p95 latency cap (ms)
RUNDAY_MAX_TIER2_LATENCY_MS_P95=5000 # Tier 2 p95 latency cap (ms)
RUNDAY_MAX_ACCEPTABLE_MISSED_PCT=5.0 # Max acceptable missed rate
RUNDAY_MIN_IV_HISTORY_COVERAGE_PCT=90.0
RUNDAY_EXPECTED_MIN_PHASE3_AUDIT_ROWS=10
RUNDAY_TELEGRAM_ON_PREFLIGHT_OK=true
RUNDAY_PIDFILE_PATH=/var/run/laabh.pid
```

---

## Failure Response Playbook

### NSE Banned IP / 403

**Symptom:** `status` shows NSE source degraded; chain NSE share drops to 0%.

**Response:**
1. `laabh-runday tier-check --filter degraded` to confirm scope
2. Check `chain_collection_issues` table for `auth_error` entries
3. Rotate the User-Agent in `.env` (`NSE_USER_AGENT=...`)
4. The review-loop will file a GitHub issue automatically
5. Dhan fallback should be absorbing collections in the meantime

---

### Phase 3 LLM Parse Failures

**Symptom:** `checkpoint phase3` fails; `llm_audit_log` shows responses but
candidate count is below target.

**Response:**
1. Query `llm_audit_log WHERE caller='fno.thesis' AND DATE(created_at)=today`
2. Inspect `response_parsed` for null entries — indicates JSON parse failure
3. Check the thesis synthesizer logs for validation errors
4. If systemic, the run will auto-retry on the next scheduler tick

---

### Position Cap Breached

**Symptom:** `status` or `checkpoint hard-exit` shows >3 open positions.

**Response:**
1. This should be impossible by construction — investigate immediately
2. `laabh-runday kill-switch --reason "position cap breach detected"`
3. Manually inspect `fno_signals` for the extra open positions
4. Check `fno_signal_events` for unexpected status transitions

---

### Chain Collection Missed Rate > 5%

**Symptom:** `status` shows red missed rate; `checkpoint` phase checks may still
pass if pipeline completed before the degradation.

**Response:**
1. `laabh-runday tier-check --filter degraded` to identify affected instruments
2. Check `source_health` for the failing source
3. If NSE is failing and Dhan is not: already in fallback mode — monitor
4. If both failing: investigate network/auth; consider kill-switch if near hard-exit time

---

### Hard Exit Did Not Complete

**Symptom:** `checkpoint hard-exit` at 14:31 shows open positions.

**Response:**
1. **Immediate:** `laabh-runday kill-switch --reason "hard exit failure"`
2. Check `fno_signals` for position statuses
3. Manually verify paper positions are recorded as closed in the DB
4. Investigate the intraday manager logs for the 14:30 exit sweep

---

## replay — Historical Dry-Run

Replay the full F&O daily routine for any historical trading date without
any real-world side effects. See [`docs/dryrun_runbook.md`](dryrun_runbook.md) for the
complete operator guide.

### Quick Reference

```bash
# Check replay prerequisites for a date
laabh-runday preflight --profile replay --date 2026-04-23

# Run the replay
laabh-runday replay --date 2026-04-23 --mock-llm --out reports/
```

### `preflight --profile` comparison

| Check | `--profile live` | `--profile replay` |
|-------|-----------------|-------------------|
| Env vars | ✅ | — |
| DB connectivity | ✅ | ✅ |
| Migrations current | ✅ | ✅ |
| Required tables | ✅ | ✅ |
| Anthropic API | ✅ | ✅ |
| Telegram | ✅ | — |
| Angel One | ✅ | — |
| NSE cookie | ✅ | — |
| Dhan live | ✅ | — |
| GitHub | ✅ | — |
| Tier table | ✅ | — |
| Trading day | ✅ | ✅ |
| Bhavcopy available | — | ✅ |

### `replay` flags

| Flag | Default | Description |
|------|---------|-------------|
| `--date` | (required) | Historical trading date YYYY-MM-DD |
| `--mock-llm` | true | Re-use cached LLM prompts |
| `--live-llm` | — | Force fresh Anthropic calls |
| `--out` | `reports/` | Output directory |
| `--json` | false | Structured JSON to stdout |

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | All gates passed |
| 10 | Gate WARN (replay completed with warnings) |
| 20 | Gate FAIL (replay aborted) |

### Suggested use after a failed live day

```bash
# After a failed live day, replay with the next day's config to inspect decisions
laabh-runday replay --date <failed-date> --mock-llm
```

---

## Testing

```bash
# Run all runday tests
pytest tests/test_runday_*.py -v

# Run with coverage
pytest tests/test_runday_*.py --cov=src/runday --cov-report=term-missing
```

All tests use mocked DB sessions and external services — no real network
calls are made.
