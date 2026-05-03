# CLAUDE-PHASE-DRYRUN.md — F&O Dry-Run Replay Module

## Overview

This phase adds a **dry-run replay capability** to Laabh: the ability to execute
the entire F&O daily routine for an arbitrary historical trading date `D`,
collapsing the day's cron schedule into a single fast local run, using
historical data sources where live data isn't replayable, and producing the
same operator-facing report the live system produces — all without sending any
real-world side effects.

The implementation is structured as a **delta on top of the existing live
routine**. The four-phase pipeline (`run_phase1`, `run_phase2`, `run_phase3`,
intraday_manager), the EOD tasks, and the runday checkpoint/report tooling are
all reused unchanged. The only new things are: an `as_of` parameter on the
small set of functions that currently call `datetime.now()`, a no-op gateway
for Telegram/GitHub/broker side-effects, a Dhan-backed historical chain source,
and a `laabh-runday replay` subcommand that orchestrates everything.

## Prerequisites

* Phases 1–4 implemented and passing tests (per `CLAUDE-PHASE-FNO.md`).
* `laabh-runday` CLI installed (per `docs/runday.md`).
* Dhan v2 account with API access — `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN`
  already configured for the live chain fallback; replay reuses these.
* TimescaleDB (or plain Postgres) with all F&O tables present.

## Goals & Non-Goals

**Goals:**

* Run the full F&O routine end-to-end for any trading date in the last 5 years
  for which Dhan has historical data.
* Reuse the production code paths verbatim wherever possible — no parallel
  implementations of Phase 1/2/3 logic.
* Reuse the runday checkpoint and report machinery — same gates, same EOD
  report layout, same surprises detection.
* Keep live-path behavior byte-identical: every code edit is a parameter
  addition with a backwards-compatible default.

**Non-Goals (v1):**

* Synthetic chain generation (no Black-Scholes fallback for missing Dhan
  history). If Dhan doesn't have the date, the replay refuses to run.
* Whisper / YouTube transcript replay (already F&O-optional).
* Multi-date sweeps or ranker-version A/B comparison (the schema is designed
  to support it later, but the v1 CLI runs one date at a time).
* Live Telegram or GitHub side-effects from a replay — explicitly suppressed.

## Architecture — what changes, what's new

```
src/
├── fno/
│   ├── chain_collector.py          [edit: as_of param]
│   ├── vix_collector.py            [edit: as_of param]
│   ├── universe.py                 [edit: as_of param threaded through]
│   ├── sources/
│   │   ├── base.py                 [unchanged]
│   │   ├── nse_source.py           [unchanged]
│   │   ├── dhan_source.py          [unchanged]
│   │   └── dhan_historical.py      [NEW — replays from Dhan v2 intraday]
│   └── orchestrator.py             [unchanged — already date-aware]
├── collectors/
│   ├── macro_collector.py          [edit: as_of param]
│   ├── fii_dii_collector.py        [edit: route to NSE archive when historical]
│   └── ...
├── services/
│   └── side_effect_gateway.py      [NEW — LiveGateway / NoOpGateway]
├── runday/
│   ├── cli.py                      [edit: new `replay` command, --profile flag]
│   ├── checks/
│   │   ├── data.py                 [edit: BhavcopyAvailableCheck]
│   │   └── pipeline.py             [edit: Phase4ManageCheck takes as_of]
│   └── ...
└── dryrun/                         [NEW package]
    ├── __init__.py
    ├── orchestrator.py             [drives the do-and-verify replay]
    ├── bhavcopy.py                 [F&O UDiFF bhavcopy fetcher + cache]
    ├── timestamps.py               [scheduled_chain_times, minute_range, etc]
    └── side_effects.py             [thin glue for NoOpGateway in replay]

database/migrations/
└── <ts>_add_dryrun_run_id.py       [NEW — adds dryrun_run_id to write tables]

docs/
├── runday.md                       [edit: add `replay` subcommand section]
└── dryrun_runbook.md               [NEW — operator guide for replay mode]
```

## Configuration Additions

Add to `src/config.py` `Settings` model (loaded from `.env`). All defaults
keep the live path unchanged.

```
# Dry-run replay
DRYRUN_ENABLED=true                          # master toggle (default true; CLI-driven)
DRYRUN_HISTORICAL_CHAIN_SOURCE=dhan          # only "dhan" supported in v1
DRYRUN_BHAVCOPY_CACHE_DIR=~/.cache/laabh/bhavcopy
DRYRUN_DHAN_CACHE_DIR=~/.cache/laabh/dhan_intraday
DRYRUN_MIN_CONTRACT_OI=1000                  # bhavcopy filter; skip illiquid contracts
DRYRUN_MIN_CONTRACT_VOLUME=100               # bhavcopy filter; skip illiquid contracts
DRYRUN_REPORT_DIR=reports                    # mirrors runday default
DRYRUN_LLM_MODE=cached_or_live               # cached_or_live | mock | live
```

## Schema Changes

A single Alembic migration adds a nullable `dryrun_run_id` column to every
table the F&O pipeline writes to. Live writes default it to NULL; replay writes
stamp it with a UUID per replay invocation. This lets multiple replays of the
same date coexist with each other and with live data, and lets the report
builder filter by run.

Tables touched:
`fno_candidates`, `fno_signals`, `fno_signal_events`, `fno_cooldowns`,
`iv_history`, `vix_ticks`, `notifications`, `llm_audit_log`,
`chain_collection_log`, `options_chain`, `job_log`.

Each gets:
```sql
ALTER TABLE <name> ADD COLUMN dryrun_run_id UUID NULL;
CREATE INDEX idx_<name>_dryrun_run_id ON <name>(dryrun_run_id) WHERE dryrun_run_id IS NOT NULL;
```

The partial index keeps the live-path query plans untouched.

---

# Sequenced Implementation Tasks

Each task below is sized to a single Claude Code prompt. Run them in order.
Each ends with self-verifying acceptance tests and an explicit documentation
update. The standard Claude Code invocation pattern is:

```
> implement Task N of CLAUDE-PHASE-DRYRUN.md
```

## Task 1 — Schema migration: dryrun_run_id columns

* **Goal**: Add a nullable `dryrun_run_id UUID` column + partial index to every
  F&O write table, with backwards-compatible defaults.
* **Files**:
  * `database/migrations/<timestamp>_add_dryrun_run_id.py` (new Alembic migration).
  * `src/models/fno_*.py`, `src/models/llm_audit.py`, `src/models/notification.py`,
    `src/models/job_log.py`, `src/models/fno_chain.py` — add `dryrun_run_id`
    Mapped column (nullable, default None).
  * `tests/test_dryrun_schema.py` (new).
* **Acceptance**:
  * `alembic upgrade head` runs cleanly on a fresh DB and on top of the existing
    Phase 1–4 schema.
  * `alembic downgrade -1` reverses cleanly.
  * Test asserts: column exists on all 11 tables; index is partial
    (`WHERE dryrun_run_id IS NOT NULL`); existing inserts without the column
    still work (backward compat).
* **Documentation**:
  * Add a "Dry-run isolation" subsection to `docs/fno_runbook.md` explaining
    the column's purpose (one paragraph).
* **Rollback**: `alembic downgrade -1`. Live path unaffected at any point.

## Task 2 — `as_of` parameter on data-fetching functions

* **Goal**: Thread an optional `as_of: datetime | None = None` parameter through
  the live data collectors and the Phase 1 chain-row reader. When `as_of` is
  None, behavior is identical to today. When set, the collector targets that
  historical timestamp and stamps written rows accordingly.
* **Files** (all edits, no new files):
  * `src/fno/chain_collector.py`: `collect_one`, `collect_tier`, `collect_all`,
    `_persist_snapshot` — accept `as_of`; when set, pass to source `fetch()`,
    and stamp `OptionsChain.snapshot_at = as_of`, `ChainCollectionLog.attempted_at = as_of`.
  * `src/fno/vix_collector.py`: `run_once(as_of=None)`. When set, fetch from
    yfinance `^INDIAVIX` history at that timestamp; stamp `vix_ticks.timestamp = as_of`.
  * `src/collectors/macro_collector.py`: `collect(as_of=None)`. When set, use
    `yf.Ticker(...).history(start=as_of-1d, end=as_of+1d)` and stamp
    `RawContent.fetched_at = as_of`.
  * `src/fno/universe.py`: `_get_atm_chain_row(session, instrument_id, *, as_of=None)`,
    `_get_avg_volume_5d(session, instrument_id, *, as_of=None)`,
    `run_phase1(run_date=None, *, as_of=None)`. When `as_of` is set, "latest
    snapshot" means latest-on-or-before `as_of`; "5d avg volume" means 5 days
    before `as_of`.
  * `src/collectors/fii_dii_collector.py`: confirm `fetch_yesterday(target_date)`
    routes to NSE archive URL when `target_date != today`. (URL format to be
    determined; the function already accepts the parameter — just make the
    archive path work.)
  * Tests: `tests/test_chain_collector_as_of.py`, `tests/test_vix_as_of.py`,
    `tests/test_macro_as_of.py`, `tests/test_phase1_as_of.py`,
    `tests/test_fii_dii_archive.py` (all new).
* **Acceptance**:
  * Every existing test in `test_fno_chain.py`, `test_fno_phase1.py`,
    `test_fno_vix.py`, `test_macro.py` still passes (zero regressions).
  * New tests verify: with `as_of` set, the source is called with the right
    arguments, the persisted timestamp matches `as_of`, and the Phase 1 chain
    query respects the cutoff.
  * `pytest -k "as_of"` is green.
* **Documentation**:
  * Add a "Replay-aware collectors" subsection to
    `src/fno/sources/context.md` (one paragraph + the parameter signature
    table).
  * Update `src/fno/context.md` with a single line under each collector
    documenting the `as_of` parameter.
* **Rollback**: revert PR. Default `None` means live path is unchanged so
  rollback is safe at any time.

## Task 3 — `SideEffectGateway` abstraction

* **Goal**: Introduce a single seam through which Telegram messages, GitHub
  issue creation, and any future broker-call side effects flow. Live mode
  delegates to existing `notification_service.NotificationService` and
  `issue_filer`; replay mode swaps in a no-op that records actions to a buffer
  for the dry-run report.
* **Files**:
  * `src/services/side_effect_gateway.py` (new). Defines:
    * `SideEffectGateway` Protocol — methods: `send_telegram(msg)`,
      `file_github_issue(title, body, labels)`, `record_capture()` (returns
      list of all actions captured so far).
    * `LiveGateway(notifier, issue_filer)` — delegates to the existing
      services.
    * `NoOpGateway()` — appends every call to an internal list, returns a
      synthetic ID.
    * `get_gateway()` / `set_gateway(g)` — context-var-backed accessor used
      by callers. Default = `LiveGateway()`.
  * Edits in `src/fno/notifications.py`, `src/fno/issue_filer.py`,
    `src/fno/orchestrator.py::_send_daily_summary` — replace direct
    instantiation of `NotificationService` with `get_gateway().send_telegram(...)`.
  * `tests/test_side_effect_gateway.py` (new).
* **Acceptance**:
  * Live path tests still pass (notifications + issue filer behave as before
    when default gateway is used).
  * `NoOpGateway` test: capture buffer accumulates entries, returns them via
    `record_capture()`, performs no real network I/O (assert via mocked `httpx`).
  * Setting a gateway via `set_gateway` is scoped to the current async context
    (uses `contextvars.ContextVar`).
* **Documentation**:
  * Add a "Side-effect gateway" subsection to `docs/fno_runbook.md`
    documenting the live vs no-op modes and where to inject.
* **Rollback**: revert PR. Default gateway preserves all existing behavior.

## Task 4 — F&O bhavcopy fetcher

* **Goal**: Fetch the NSE F&O UDiFF bhavcopy zip for any historical date and
  return a normalized DataFrame of contracts with their EOD OHLC + OI. This is
  the source of truth for "which option contracts existed on date D".
* **Files**:
  * `src/dryrun/bhavcopy.py` (new). Defines:
    * `async def fetch_fo_bhavcopy(d: date) -> pd.DataFrame` — downloads
      `https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{YYYYMMDD}_F_0000.csv.zip`,
      unzips, parses, returns columns:
      `[symbol, instrument, expiry_date, strike_price, option_type, open, high,
       low, close, settle_price, contracts, value_in_lakh, oi, change_in_oi,
       last_price]`.
    * `async def fetch_cm_bhavcopy(d: date) -> pd.DataFrame` — same for cash
      market: `BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip` (used to
      backfill `price_daily` for replay if missing).
    * Disk cache under `DRYRUN_BHAVCOPY_CACHE_DIR`. 404 → `BhavcopyMissingError`.
    * Browser-mimicking headers (same UA pattern as `nse_source.py`).
  * `tests/test_dryrun_bhavcopy.py` (new) — uses `respx` to mock NSE archive
    responses; one test for happy path, one for 404, one for cache hit.
* **Acceptance**:
  * `pytest tests/test_dryrun_bhavcopy.py` is green.
  * Cache hit on second invocation does not re-download (assert via mock call
    count).
  * Returned DataFrame has all expected columns and at least one CE+PE row per
    listed underlying.
* **Documentation**:
  * Add a "Bhavcopy reader" section to `docs/dryrun_runbook.md` (created in
    Task 10) — but stub the file in this task with just this section.
* **Rollback**: revert PR. Module is only imported by replay code (not live).

## Task 5 — Dhan historical chain source

* **Goal**: Implement a `BaseChainSource` adapter that, given a `(symbol,
  expiry_date, as_of)` triple, reconstructs an option chain snapshot from
  Dhan v2's `/v2/charts/intraday` endpoint with `oi:true`. Filters contracts
  by liquidity using the bhavcopy from Task 4.
* **Files**:
  * `src/fno/sources/dhan_historical.py` (new). Implements:
    * `class DhanHistoricalSource(BaseChainSource)` with `name = "dhan_historical"`.
    * Constructor takes a date `D` and pre-loads the F&O bhavcopy for `D`
      (filtered to `oi >= DRYRUN_MIN_CONTRACT_OI` AND
      `volume >= DRYRUN_MIN_CONTRACT_VOLUME`).
    * `async def fetch(symbol, expiry_date, *, as_of) -> ChainSnapshot`:
      1. Look up active strikes from the cached bhavcopy for `(symbol, expiry_date)`.
      2. For each strike × {CE, PE}, look up Dhan `security_id` via the Dhan
         instrument master JSON (loaded once per `D`).
      3. Call `/v2/charts/intraday` with `interval="5"`, `oi:true`,
         `fromDate=as_of-1h`, `toDate=as_of`. Pick the candle whose timestamp
         is the latest at or before `as_of`.
      4. Look up the underlying's LTP at `as_of` from a parallel intraday call
         on the underlying's security_id.
      5. Construct `StrikeRow` per contract with native Dhan greeks/IV.
      6. Return `ChainSnapshot(symbol, expiry_date, underlying_ltp, snapshot_at=as_of, strikes=...)`.
    * Disk cache under `DRYRUN_DHAN_CACHE_DIR/{D}/{security_id}_{interval}.json`
      so a second replay of the same date doesn't re-hit Dhan.
    * Rate limiting: respect Dhan's 100k/day budget; minute timeframes have no
      per-second cap. Concurrency capped via an `asyncio.Semaphore`.
  * `tests/test_dryrun_dhan_historical.py` (new) — uses `respx` to mock Dhan
    endpoints. Covers: happy path with 3 strikes × 2 sides, missing
    security_id (skipped silently), cache hit, candle picking when `as_of`
    falls between two candles.
* **Acceptance**:
  * Tests are green.
  * `health_check()` returns True when Dhan credentials are valid.
  * Returned `ChainSnapshot` shape is byte-identical to what `DhanSource`
    returns in the live path (existing `chain_parser` consumes it without
    changes).
* **Documentation**:
  * Append a "Historical adapter" section to `src/fno/sources/context.md`
    documenting the new source, its inputs, caching behavior, and rate
    limiting.
* **Rollback**: revert PR. Source is only registered when replay mode is
  active.

## Task 6 — runday: replay-profile preflight + bhavcopy availability check

* **Goal**: Extend `laabh-runday preflight` to support a `--profile {live,replay}`
  flag. Replay profile drops Telegram/GitHub/Angel-One/NSE-cookie checks
  (irrelevant for replay), keeps DB/schema/Anthropic, and adds a
  `BhavcopyAvailableCheck` that 404-tests the F&O bhavcopy URL for the target
  date.
* **Files**:
  * `src/runday/checks/data.py`: add `class BhavcopyAvailableCheck` that calls
    `bhavcopy.fetch_fo_bhavcopy(D)` and reports OK/FAIL.
  * `src/runday/cli.py`: add `--profile` and `--date` to the `preflight`
    command. Live profile = current behavior. Replay profile = subset described
    above + `BhavcopyAvailableCheck(date=D)`.
  * `tests/test_runday_replay_preflight.py` (new).
* **Acceptance**:
  * `laabh-runday preflight --profile live` is unchanged in behavior.
  * `laabh-runday preflight --profile replay --date 2026-04-23` runs the
    replay-profile checks; 0 FAILs on a date where bhavcopy exists; non-zero
    exit when bhavcopy is 404.
  * Existing preflight tests still pass.
* **Documentation**:
  * Edit `docs/runday.md` `preflight` section: document `--profile` flag and
    add a small table comparing live vs replay check sets.
* **Rollback**: revert PR. The `--profile` flag defaults to `live` so existing
  invocations behave identically.

## Task 7 — `Phase4ManageCheck` accepts simulated `now`

* **Goal**: Allow `Phase4ManageCheck` to take a simulated `now: datetime | None`
  so it can validate manage-loop activity within a replayed market window.
* **Files**:
  * `src/runday/checks/pipeline.py`: `Phase4ManageCheck.__init__` accepts
    `now: datetime | None = None`. When set, the market_open/close window is
    constructed for `self._anchor` and the bracket comparison uses `now`.
    When None, behavior is unchanged (uses `datetime.now(UTC)`).
  * `tests/test_runday_phase4_manage_simulated_now.py` (new).
* **Acceptance**:
  * Existing `test_runday_pipeline.py` tests pass.
  * New test: with `anchor_date=D` and `now=ist(D, 14, 25)`, the check returns
    OK if a manage job_log row exists at `D 14:22 IST`.
* **Documentation**:
  * Add a one-liner to the `Phase4ManageCheck` docstring explaining the
    simulated-time mode.
* **Rollback**: revert PR. Default-`None` `now` keeps live behavior unchanged.

## Task 8 — Dry-run orchestrator

* **Goal**: The single function that drives a replay end to end. Calls existing
  pipeline functions with `run_date=D`/`as_of=ts`, gates each step using
  `make_phase_check`, and returns a structured result.
* **Files**:
  * `src/dryrun/orchestrator.py` (new). Defines:
    * `async def replay(D: date, *, mock_llm: bool, run_id: UUID) -> ReplayResult`
      — pseudocode shape:
      ```
      with set_gateway(NoOpGateway()):
          with set_dryrun_run_id(run_id):
              # Stage 1 — pre-flight (replay profile)
              _gate(await BhavcopyAvailableCheck(s, D).run())
              # plus DBConnectivity, RequiredTables, TradingDay(D)

              # Stage 2 — register DhanHistoricalSource for this run
              with replay_chain_source(D):
                  for ts in scheduled_chain_times(D):     # 09:00..15:30 every 5 min
                      await chain_collector.collect_tier(1, as_of=ts)
                      if ts.minute % 15 == 0:
                          await chain_collector.collect_tier(2, as_of=ts)
                      await vix_collector.run_once(as_of=ts)
                  for ts in scheduled_macro_times(D):     # 06:00..09:00 every 15 min
                      await macro_collector.collect(as_of=ts)

              await ban_list.fetch_today(ban_date=D)
              await fii_dii_collector.fetch_yesterday(target_date=D - 1d)

              # Stage 3 — Phases 1–3 (live function, unchanged)
              await orchestrator.run_premarket_pipeline(D)
              for phase in ["phase1", "phase2", "phase3"]:
                  _gate(await make_phase_check(phase, s, D).run())

              # Stage 4 — Phase 4 tick loop
              state = IntradayState()
              for ts in minute_range(D, 9, 15, 14, 30):
                  await intraday_manager.entry_tick(now=ts, state=state, run_id=run_id)
                  await intraday_manager.manage_tick(now=ts, state=state, run_id=run_id)
              await intraday_manager.hard_exit_all(now=ist(D, 14, 30), state=state)
              _gate(await make_phase_check("hard-exit", s, D).run())

              # Stage 5 — EOD (live function, unchanged)
              await orchestrator.run_eod_tasks(D)
              for phase in ["iv-history", "ban-list"]:
                  _gate(await make_phase_check(phase, s, D).run())

              return ReplayResult(run_id=run_id, gates_passed=..., captures=NoOpGateway().record_capture())
      ```
    * `replay_chain_source(D)` is a context manager that swaps the source
      registry in `chain_collector` to use `DhanHistoricalSource(D)` for the
      duration.
    * `_gate(check_result)` raises `ReplayGateFailed` on FAIL severity, with
      a message identifying which check broke and where to look.
  * `src/dryrun/timestamps.py` (new) — pure helpers: `scheduled_chain_times(D)`,
    `scheduled_macro_times(D)`, `minute_range(D, h1, m1, h2, m2)`,
    `ist(D, h, m)`.
  * `src/dryrun/side_effects.py` (new) — thin wrappers `set_dryrun_run_id`,
    `set_gateway` context managers.
  * `tests/test_dryrun_orchestrator.py` (new) — heavy use of mocks. Covers:
    * Successful end-to-end run on a synthetic date with stubbed Dhan / Phase 1–4
      results; asserts every existing live function was called with the right
      `run_date`/`as_of`.
    * Phase 1 gate failure aborts the run cleanly.
    * Replay never sends a Telegram message (assert via `NoOpGateway.record_capture`).
* **Acceptance**:
  * Tests green.
  * No new dependency between live code and `src/dryrun/` — the orchestrator
    only imports from live modules, never the reverse.
* **Documentation**:
  * Append "Replay orchestration" section to `docs/dryrun_runbook.md` showing
    the stage diagram and gate logic.
* **Rollback**: revert PR. Live path is unaware of this module.

## Task 9 — `laabh-runday replay` CLI subcommand

* **Goal**: Wire the orchestrator behind a single CLI verb consistent with the
  existing runday command set.
* **Files**:
  * `src/runday/cli.py`: add `replay` typer command:
    ```
    laabh-runday replay --date YYYY-MM-DD
                        [--mock-llm | --live-llm]
                        [--out reports/]
                        [--json]
    ```
  * After `replay` returns a `ReplayResult`:
    1. Call `daily_report.build_report(D, dryrun_run_id=run_id)` — the existing
       builder, parameterized by run_id (one-line edit to the SQL filters in
       `daily_report.py`).
    2. Write `reports/replay-{D}-{run_id_short}.md` via existing
       `format_markdown_report`.
    3. Print summary to console using existing `_render_report_console`.
  * Edit `src/runday/scripts/daily_report.py::build_report` to accept an
    optional `dryrun_run_id` argument that adds `WHERE dryrun_run_id = :run_id`
    to every query (defaults to `IS NULL` which preserves live behavior).
  * `tests/test_runday_replay_cli.py` (new) — uses `CliRunner` from typer to
    invoke `replay --date 2026-04-23` with `orchestrator.replay` mocked.
* **Acceptance**:
  * `laabh-runday --help` lists `replay` alongside `preflight`/`checkpoint`/
    `status`/`report`.
  * `laabh-runday replay --date 2026-04-23 --mock-llm --out /tmp/reports/`
    produces `/tmp/reports/replay-2026-04-23-{shortid}.md`.
  * Exit codes: 0 (clean), 10 (gate WARN), 20 (gate FAIL).
  * Existing `report` command still works on live data when `dryrun_run_id` is
    not supplied.
* **Documentation**:
  * Add `replay` subcommand to `docs/runday.md` reference, with flag table
    and exit code table.
  * Cross-reference `docs/dryrun_runbook.md` from the new section.
* **Rollback**: revert PR. The `replay` command becoming unavailable does not
  affect live operations.

## Task 10 — Documentation pass

* **Goal**: Stand up `docs/dryrun_runbook.md` as the operator-facing guide,
  finalize edits to `docs/runday.md`, and add a brief mention to `README.md`.
* **Files** (all docs):
  * `docs/dryrun_runbook.md` (new). Sections:
    1. **Overview** — what dry-run is, when to use it.
    2. **Quick start** — three-command worked example.
    3. **What gets replayed** — table of phases vs reuse status (the audit
       from this conversation, condensed).
    4. **What gets suppressed** — Telegram, GitHub issues, broker calls.
    5. **Source coverage** — Tier A/B/C/D table from the source audit, with
       the explicit "Tier C gaps proceed with no_data annotation" rule.
    6. **CLI reference** — `laabh-runday replay` flags, exit codes,
       worked invocations.
    7. **Output report** — sample `reports/replay-*.md` excerpt + how to
       diff two replays of the same date.
    8. **Troubleshooting** — the bhavcopy-404 case, the Dhan-no-history
       case, the Phase 1 empty-universe case, what each gate failure means.
    9. **Cost notes** — Dhan API budget, Anthropic LLM cost (with
       `--mock-llm` recommendation).
  * `docs/runday.md`: insert `replay` subcommand in the subcommand reference
    in CLI order; update the "Suggested Daily Flow" to mention "after a
    failed live day, replay it tomorrow with `laabh-runday replay --date <D>`
    to inspect what would have happened with new config".
  * `README.md`: append a one-line bullet under "Phased Build Plan" pointing
    at `docs/dryrun_runbook.md`.
  * `CLAUDE.md` (top-level): one-line note in Rules-for-Claude-Code that
    every new pipeline-mutating function should accept `as_of` and
    `dryrun_run_id` parameters by convention.
* **Acceptance**:
  * `mkdocs build` (or whatever the project uses) — no broken cross-refs.
  * Manually verify each example invocation in `dryrun_runbook.md` runs
    successfully against the implementation.
* **Documentation**: This task **is** the documentation. No further docs
  update needed.
* **Rollback**: revert PR.

---

# Testing Strategy

* **Unit**: each task ships its own test module. Total expected new tests:
  ~12 modules, ~80 tests.
* **Integration**: `tests/integration/test_dryrun_end_to_end.py` runs a full
  replay against a small synthetic universe (3 underlyings, 1 expiry, 5
  strikes/side) with a stubbed Dhan adapter that returns canned candles.
  Asserts:
  * Every existing live function is invoked exactly the number of times the
    schedule prescribes.
  * Final `replay-{D}.md` contains all expected sections.
  * No Telegram or GitHub side-effect was attempted.
  * `dryrun_run_id` is stamped on every written row.
* **Smoke** (manual): `laabh-runday replay --date <recent-real-date> --mock-llm`
  runs to completion locally against a real Dhan account.

# Open Questions / Assumptions

1. **Dhan instrument master for historical dates.** Dhan publishes a
   day-current instrument master. For replay, we may need to query the
   "expired options" endpoint for contracts that have since expired. Task 5
   should confirm and document the exact endpoint at implementation time.
2. **NSE FII/DII archive URL.** The live `fii_dii_collector` hits
   `/api/fiidiiTradeReact` (current-only). Task 2's note "route to NSE
   archive when historical" needs the actual archive URL — to be discovered
   during implementation by inspecting NSE archive listings for known dates.
3. **`as_of` for fii_dii**. FII/DII data lags by one trading day. Replay
   should call `fetch_yesterday(target_date=D - 1d)` not `D` itself —
   confirm in Task 8 and add a comment in the code.
4. **Concurrency in chain replay.** `scheduled_chain_times(D)` produces ~75
   timestamps for a normal day. Within each timestamp, `collect_tier` already
   iterates instruments serially. The simplest v1 keeps this serial; if a
   replay takes >5 minutes wall-clock, parallelize across timestamps using
   `asyncio.gather` with a semaphore.
5. **Default LLM mode.** v1 default = `cached_or_live`: re-use audit log if
   prompts match; else hit Anthropic. Operators can pin `--mock-llm` for cost
   discipline or `--live-llm` to force fresh calls during prompt iteration.

# What Success Looks Like

After this phase, the following workflow works locally on a developer
machine:

```bash
$ laabh-runday preflight --profile replay --date 2026-04-23
🟢 All replay-profile checks passed.

$ laabh-runday replay --date 2026-04-23 --mock-llm --out reports/
Stage 1 (pre-flight): ✅
Stage 2 (chain replay 09:00..15:30, every 5 min): ✅ 75 snapshots, 0 misses
Stage 3 (phases 1–3): ✅ phase1=42 phase2=20 phase3=10
Stage 4 (phase 4 tick loop): ✅ 3 entries, 2 closed_target, 1 closed_stop
Stage 5 (EOD): ✅ iv_history=180 ban_list=2

P&L: +₹4,250 | Surprises: 0 | Captured Telegrams: 6 (suppressed)
Report: reports/replay-2026-04-23-a1b2c3d4.md
```

— and the resulting markdown is byte-equivalent in section structure to a
real `runday-2026-04-23.md`, just with `dryrun_run_id` stamped throughout
and zero real-world side effects emitted.