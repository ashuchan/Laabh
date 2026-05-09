# Quant Backtest Harness — Runbook

This runbook covers operational use of the real-data backtest harness
introduced by `CLAUDE-FNO-TASK-QUANT-BACKTEST.md`. It targets an engineer
who wants to (a) bulk-load historical data, (b) replay a date range
through the orchestrator, (c) interpret the output, and (d) reconcile
backtest vs live paper-trading.

## 1. Prerequisites

* Postgres + TimescaleDB reachable; `DATABASE_URL` set in `.env`.
* `alembic upgrade head` has been run (the migration `0014_quant_backtest_tables`
  creates the four backtest tables).
* For Tier 1 intraday data: a Dhan Data API subscription with `DHAN_CLIENT_ID`,
  `DHAN_PIN`, `DHAN_TOTP_SECRET` set in `.env`. Without it, daily-resolution
  backtests still work (Tier 0/2 only) but intraday primitives degrade.
* Python 3.12+; project deps installed via `pip install -e .`.

## 2. One-time data load

The four loaders below are independent. Run them in any order, but the
suggested sequence reflects the speed of each:

```bash
# 2a. RBI repo rate (small CSV, < 50 rows). Provide the CSV path.
python -c "
import asyncio
from src.quant.backtest.data_loaders.rbi_repo_history import load_from_csv
asyncio.run(load_from_csv('data/rbi_repo.csv', source='rbi.org.in'))
"

# 2b. F&O ban list (NSE archives). Backfill 12 months.
python -c "
import asyncio
from datetime import date
from src.quant.backtest.data_loaders.nse_ban_list_history import backfill
asyncio.run(backfill(date(2025, 5, 1), date(2026, 5, 1)))
"

# 2c. India VIX (yfinance ^INDIAVIX). One row per day at 15:30 IST.
python -c "
import asyncio
from datetime import date
from src.quant.backtest.data_loaders.nse_vix_history import backfill
asyncio.run(backfill(date(2025, 5, 1), date(2026, 5, 1)))
"

# 2d. F&O bhavcopy (NSE archives). 5,000+ rows per trading day. Slow.
python -c "
import asyncio
from datetime import date
from src.quant.backtest.data_loaders.nse_bhavcopy import backfill
asyncio.run(backfill(date(2025, 5, 1), date(2026, 5, 1)))
"

# 2e. Dhan intraday OHLC (1-min bars). Largest data load — 30 req/min limit.
#     Resumable: re-run after a failure and it picks up from MAX(timestamp).
python -c "
import asyncio
from datetime import date
from src.quant.backtest.data_loaders.dhan_historical import backfill, load_universe_from_db
async def main():
    instruments = await load_universe_from_db(only_fno=True)
    await backfill(
        instruments=instruments,
        start_date=date(2025, 5, 1),
        end_date=date(2026, 5, 1),
    )
asyncio.run(main())
"
```

Expect 2d to take ~15 minutes for one year of bhavcopy.

For 2e (Dhan intraday), arithmetic on the rate-limit budget:
30 req/min × 60 min/hr = 1,800 req/hr. A full F&O universe is ~200 instruments,
and one-year coverage is ~250 trading days, giving 50,000 calls total ≈ **28 hours
of wall clock** under the default budget. In practice, plan for 1–2 days
end-to-end (allow for retries on transient errors). Resume support means
you can stop and restart freely — the loader picks up from `MAX(timestamp)`
per instrument. Consider running it on a dedicated machine over a weekend.

## 3. Run a backtest

```bash
python -m scripts.backtest_run \
    --start-date 2026-04-01 \
    --end-date 2026-04-30 \
    --portfolio-id <portfolio-uuid> \
    --seed 42
```

The script prints a per-day P&L table at the end. Every `backtest_runs` row
created during the run is queryable in Postgres for further analysis.

### Reproducibility

Same `--seed` with the same data → bit-identical results. The seed flows into
`bandit_seed` on every `backtest_runs` row. The orchestrator's
`_seed_for_arm(portfolio_id, arm_id, day)` is SHA-256-derived (not Python's
salted `hash`), so reproducibility holds even across processes.

### Smile method override

```bash
--smile-method flat     # cheap and crude — every strike uses ATM IV
--smile-method linear   # default — slope estimated from prior day's chain
--smile-method sabr     # NotImplementedError; deferred to v2
```

## 4. Interpret the report

The CLI summary surfaces:

* **Cumulative P&L** — geometric chain of per-day pnl_pct.
* **Trades** — sum of `backtest_trades.realized_pnl > 0` count vs total.
* **Per-day table** — start/final NAV, P&L %, trade count, status.

For deeper metrics (Sharpe, deflated Sharpe, max drawdown, walk-forward
windows) call the reporting module directly:

```python
from src.quant.backtest.reporting.metrics import compute_metrics
returns = [...]  # daily pnl_pct from backtest_runs
bundle = compute_metrics(returns, n_trials=20, bootstrap_iter=1000)
print(bundle.sharpe, bundle.deflated_sharpe, bundle.sharpe_ci_lower)
```

### Headline metric: Deflated Sharpe Ratio

`deflated_sharpe(returns, n_trials=N)` returns a probability in [0, 1] —
the probability that the *true* Sharpe exceeds 0 given the observed
series, after correcting for:

* Multiple-testing inflation (`n_trials`)
* Higher moments of the return distribution (skew + kurtosis)

A value > 0.95 is conventionally "significant"; < 0.5 means the strategy
looks no better than the best of `n_trials` random sentinels.

### Walk-forward validation

```python
from datetime import date
from src.quant.backtest.runner import BacktestRunner
from src.quant.backtest.reporting.walk_forward import compute_windows, run_walk_forward

runner = BacktestRunner(portfolio_id=..., seed=42)
windows = compute_windows(
    start_date=date(2025, 5, 1),
    end_date=date(2026, 5, 1),
    train_days=60,
    test_days=20,
    purge_days=5,
)
result = await run_walk_forward(runner=runner, windows=windows)
print("Median test-set Sharpe:", result.median_sharpe)
print("RED FLAG:", result.red_flag)  # True if median < 0
```

## 5. Reconcile backtest ↔ live paper-trading

After a week of live quant-mode trading, run:

```bash
python -m scripts.backtest_compare_to_paper \
    --portfolio-id <portfolio-uuid> \
    --start-date 2026-04-27 \
    --end-date 2026-05-09
```

The script prints:

* **Fidelity score** — `1 - mean(|Δ pnl|) / mean(|live_pnl|)`. 1.0 = perfect
  match; 0.0 = backtest is misleading.
* **Per-date P&L table** — live, backtest, delta.
* **Per-arm count deltas** — for each arm, how many trades fired in each mode.
* **First 10 trade-level diffs** — trades present in only one ledger.

A fidelity score < 0.7 is a yellow flag and < 0.5 is red — the backtest's
synthesized chain is not capturing whatever the live mode is reacting to.

## 6. Known limitations

* **OFI primitive is excluded from backtest**. L1 quote-size deltas aren't
  in retail historical data. The OFI primitive returns no signal in
  backtest mode and must be validated in live-shadow mode separately.
* **Intraday IV is held flat at the morning's ATM IV** — Tier 3 chain
  synthesis. Real intraday IV moves are not captured.
* **No real bid-ask spreads** — synthesized chain applies a 0.3% spread
  centered on the BS premium. Liquid Nifty options run 0.1–0.5%; mid-cap
  stock options run 1–3%.
* **OI evolves only end-of-day** — the synthesized chain has zero OI;
  consumers needing OI read it from the Tier 2 close-of-day chain.
* **VIX is daily** — sourced from yfinance close. Real intraday VIX moves
  are not captured.
* **Fixed risk-free rate** — defaults to 6.5% if `rbi_repo_history` is
  empty; otherwise looks up the most recent rate at-or-before the trading
  date.

## 7. Troubleshooting

### "no candidates found for D — is price_daily populated for the prior 5 days?"

The `TopGainersUniverseSelector` needs at least 2 days of `price_daily`
history before the trading date. Run `scripts.backfill_price_daily_changes`
or wait for the daily collector to fill the gap.

### Dhan rate limit (HTTP 429)

The loader's `_RateLimiter` defaults to 30 requests/minute. If Dhan tightens
the limit, lower the rate via:

```python
await backfill(..., rate_limit_per_min=20)
```

### "BhavcopyMissingError: NSE archive 404"

Holidays and weekends. The bhavcopy loader skips them silently; if you see
this for a known trading day, check NSE's archive URL pattern hasn't
changed (it has changed twice since 2024).

### "no parseable rows in <CSV>"

The RBI repo CSV must have header `date,repo_rate_pct` and ISO dates
(YYYY-MM-DD). Junk rows (header text, blank lines) are skipped silently.

### LookaheadViolation raised mid-replay

`src.quant.backtest.checks.lookahead.LookaheadGuard` caught a feature
read targeting a timestamp strictly after the virtual clock. Inspect the
stack trace; the bug is likely a primitive doing `df.shift(-N)` or a
feature-store query without a `<=` cutoff.

## 8. CI gate (recommended)

Add to your CI pipeline:

* **Smoke benchmark**: 1 trading day, 5-instrument universe, < 20s wall.
  Fails if exceeded by > 25%.
* **Lookahead detector enabled** in any benchmark run — `LookaheadGuard`
  with `raise_on_violation=True`. A passing benchmark proves no lookahead.
* **Static grep for `df.shift(-...)`** in `src/quant/primitives/`. Spec §13
  calls for this; not yet automated.

## 9. Runbook checklist for a new engineer

1. Clone the repo; `pip install -e .`
2. Run `alembic upgrade head` against the dev database.
3. Run section 2 to backfill ~3 months of recent data (faster than 1 year).
4. Run section 3 over a 5-day window and verify the per-day table.
5. Run section 5 against any week of live paper-trading — fidelity > 0.5
   confirms the harness is working.
6. Read `metrics.py` and `compare_modes.py` source — they're 200-300 LOC
   each, and the formulas are the only knobs you might want to change.

If the workflow doesn't feel like ~2-4 minutes per single-config 1-month
backtest after the perf patch lands (Track 16C parallelism, etc.), the
patch hasn't done its job — escalate.
