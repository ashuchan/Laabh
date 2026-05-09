"""Quant-mode backtest harness.

Replays historical Indian-market data through the same orchestrator that runs
live, with I/O implementations swapped for historical equivalents:

  * BacktestClock           — virtual time, advances tick-by-tick (this module → clock.py)
  * BacktestFeatureStore    — historical feature lookup (Task 8)
  * TopGainersUniverseSelector — deterministic universe (Task 6)
  * BacktestRunner          — top-level CLI entry (Task 10)

See `docs/quant_backtest_runbook.md` for usage.
"""
