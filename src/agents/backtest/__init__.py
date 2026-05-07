"""Backtest utilities for agentic workflows.

Reusable utility for running an agent workflow against historical (or today's)
market state in a non-destructive dry-run mode and analyzing the result.

Three pieces:
  * `MockAnthropicClient` — schema-valid stub responses, zero API cost.
  * `MarketSnapshot` — read-only view of DB state at `as_of`.
  * `BacktestRunner` — wraps `WorkflowRunner` with mock-LLM, no-op DB writes,
    captured side-effects, and a structured `BacktestResult`.

Entry points:
    python -m src.agents.backtest --date 2026-05-07
    python -m src.agents.backtest --date 2026-05-07 --workflow predict_today_combined
    python -m src.agents.backtest --date 2026-05-07 --live-llm   # burns API budget

Library use:
    from src.agents.backtest import BacktestRunner
    runner = BacktestRunner.create_default()
    result = await runner.run("predict_today_combined", as_of=date(2026, 5, 7))
"""
from __future__ import annotations

from src.agents.backtest.mock_anthropic import MockAnthropicClient
from src.agents.backtest.runner import BacktestResult, BacktestRunner
from src.agents.backtest.snapshot import MarketSnapshot, fetch_snapshot
from src.agents.backtest.report import render_backtest_report
from src.agents.backtest.transcript import write_transcript

__all__ = [
    "BacktestRunner",
    "BacktestResult",
    "MarketSnapshot",
    "MockAnthropicClient",
    "fetch_snapshot",
    "render_backtest_report",
    "write_transcript",
]
