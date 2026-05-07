#!/usr/bin/env python3
"""Diagnostic probe — exercise every SQL-backed agent tool with realistic input
and report which ones fail and why. Read-only.

Usage:
    python scripts/probe_agent_tools.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))


async def main() -> None:
    from sqlalchemy import text
    from src.agents.tools.registry import activate_sql_executors, TOOL_REGISTRY
    from src.db import get_session_factory

    activate_sql_executors()
    factory = get_session_factory()

    # Resolve a real RELIANCE UUID + an analyst id for the calls that need them.
    async with factory() as s:
        rel = (await s.execute(text(
            "SELECT id, symbol FROM instruments WHERE symbol='RELIANCE' LIMIT 1"
        ))).fetchone()
        analyst = (await s.execute(text("SELECT id FROM analysts LIMIT 1"))).fetchone()

    rel_uuid = str(rel[0]) if rel else None
    analyst_id = str(analyst[0]) if analyst else None
    since_iso = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    today = datetime.now(timezone.utc)

    ctx = SimpleNamespace(db=factory, as_of=today, is_replay=False)

    # name → params (simulating realistic LLM-issued tool calls)
    cases: list[tuple[str, dict]] = [
        ("search_raw_content", {"instrument_id": rel_uuid, "since": since_iso}),
        ("get_filings", {"instrument_id": rel_uuid, "since": since_iso}),
        ("search_transcript_chunks", {"symbol": "RELIANCE", "since": since_iso}),
        ("get_analyst_track_record", {"analyst_id": analyst_id}),
        ("get_price_aggregates", {"instrument_id": rel_uuid, "window": "1d_daily_60"}),
        ("get_past_predictions", {"instrument_id": rel_uuid}),
        ("get_sentiment_history", {"instrument_id": rel_uuid, "since": since_iso}),
        ("get_options_chain", {"underlying_id": rel_uuid, "expiry_date": "2026-05-29"}),
        ("get_iv_context", {"underlying_id": rel_uuid}),
        ("enumerate_eligible_strategies", {
            "direction": "bullish", "iv_regime": "fair",
            "expiry_days": 22, "vix_regime": "neutral"
        }),
        ("get_strategy_payoff", {
            "strategy_name": "bull_call_spread",
            "legs": [{"action": "BUY", "type": "CE", "strike": 1280, "lots": 1}],
            "expiry_date": "2026-05-29",
            "spot": 1280.0, "iv_input": 0.22,
        }),
        ("check_ban_list", {"instrument_id": rel_uuid}),
        ("score_technicals", {"instrument_id": rel_uuid}),
        ("score_fundamentals", {"instrument_id": rel_uuid}),
        ("position_sizing", {
            "account_value_inr": 100000.0, "target_pct": 4.0,
            "stop_pct": 2.0, "conviction": 0.6,
        }),
        ("get_full_rationale", {"prediction_or_candidate_id": "00000000-0000-0000-0000-000000000000"}),
    ]

    print(f"{'TOOL':<35} {'STATUS':<8} DETAIL")
    print("-" * 110)
    for tool_name, params in cases:
        td = TOOL_REGISTRY[tool_name]
        try:
            result = await td.executor(params, ctx)
        except Exception as e:
            print(f"{tool_name:<35} CRASH    {type(e).__name__}: {str(e)[:100]}")
            continue

        if isinstance(result, dict) and "error" in result:
            err = result["error"]
            err = err.split("\n")[0][:120]
            print(f"{tool_name:<35} ERROR    {err}")
        else:
            n = result.get("count")
            score = result.get("score")
            res = result.get("result")
            summary = (
                f"count={n}" if n is not None
                else f"score={score}" if score is not None
                else f"keys={list(result.keys())[:5]}" if isinstance(result, dict) and result
                else "<empty>"
            )
            print(f"{tool_name:<35} OK       {summary}")


if __name__ == "__main__":
    asyncio.run(main())
