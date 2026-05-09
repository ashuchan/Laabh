"""Historical data loaders for the quant backtest harness.

Each loader is responsible for backfilling one historical table. All are
idempotent (re-running over the same date range inserts zero new rows) and
designed to be invoked from ``scripts/backtest_load_data.py``.

Modules:
  * dhan_historical          — Tier 1 intraday OHLC (Task 2)
  * nse_bhavcopy             — Tier 2 daily option chain snapshots (Task 3)
  * nse_vix_history          — India VIX time series (Task 4)
  * nse_ban_list_history     — Daily F&O ban list (Task 4)
  * rbi_repo_history         — RBI repo rate (Task 4, used as risk-free rate)
"""
