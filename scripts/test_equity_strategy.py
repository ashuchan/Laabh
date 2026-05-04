"""Smoke-test the equity strategy without scheduling.

Usage:
    python scripts/test_equity_strategy.py morning
    python scripts/test_equity_strategy.py intraday
    python scripts/test_equity_strategy.py eod

    # Replay against a historical date (uses signals/holdings/decisions filtered
    # to that date; LTPs still come from price_ticks — see CAVEAT below).
    python scripts/test_equity_strategy.py morning --as-of 2026-05-04

    # --dryrun stamps the run with a UUID so the DB rows it creates are easy
    # to delete afterwards. Implies --no-telegram.
    python scripts/test_equity_strategy.py morning --as-of 2026-05-04 --dryrun

    # --no-telegram blanks the bot token for this process so notifications
    # are written to DB but never pushed.
    python scripts/test_equity_strategy.py intraday --no-telegram

CAVEAT: PriceService.latest_price() always returns the most recent tick in
``price_ticks``. For a true historical replay (prices as they were on that
date) you need to plumb through src/dryrun/orchestrator.py — this smoke test
only validates the *decision flow* against historical signals/holdings/cash.

Requires:
    EQUITY_STRATEGY_ENABLED=true in .env (or env var)
    ANTHROPIC_API_KEY set
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from datetime import datetime, time, timezone

from loguru import logger


def _parse_as_of(s: str) -> datetime:
    """Accept YYYY-MM-DD (interpreted as 09:10 IST that date) or full ISO."""
    if "T" in s:
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    # Date-only → 09:10 IST that day → UTC (IST = UTC+5:30, so 03:40 UTC)
    d = datetime.fromisoformat(s).date()
    return datetime.combine(d, time(3, 40), tzinfo=timezone.utc)


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["morning", "intraday", "eod"])
    p.add_argument("--as-of", dest="as_of", help="YYYY-MM-DD or ISO datetime")
    p.add_argument("--dryrun", action="store_true", help="stamp rows with a dryrun_run_id")
    p.add_argument("--no-telegram", action="store_true", help="suppress Telegram pushes")
    p.add_argument(
        "--telegram",
        action="store_true",
        help="force Telegram pushes even when used with --dryrun",
    )
    args = p.parse_args()

    if args.no_telegram and args.telegram:
        p.error("--telegram and --no-telegram are mutually exclusive")
    if args.no_telegram:
        os.environ["TELEGRAM_BOT_TOKEN"] = ""
        logger.info("telegram suppressed for this run")
    elif args.dryrun and not args.telegram:
        logger.info(
            "dryrun: telegram still ENABLED — pass --no-telegram to suppress"
        )

    as_of = _parse_as_of(args.as_of) if args.as_of else None
    dryrun_run_id = uuid.uuid4() if args.dryrun else None

    if dryrun_run_id:
        logger.info(f"dryrun_run_id = {dryrun_run_id}")
        logger.info(
            "to clean up afterwards:\n"
            f"  DELETE FROM strategy_decisions WHERE dryrun_run_id = '{dryrun_run_id}';\n"
            f"  DELETE FROM trades WHERE entry_reason LIKE '%{dryrun_run_id}%';"
        )

    from src.trading.strategy_runner import (
        run_eod_squareoff,
        run_intraday_action,
        run_morning_allocation,
    )

    fn = {
        "morning": run_morning_allocation,
        "intraday": run_intraday_action,
        "eod": run_eod_squareoff,
    }[args.action]

    result = await fn(as_of=as_of, dryrun_run_id=dryrun_run_id)
    logger.info(f"result: {result}")


if __name__ == "__main__":
    asyncio.run(main())
