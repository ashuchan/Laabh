"""Backfill historical daily OHLCV from yfinance for all active instruments."""
from __future__ import annotations

import argparse
import asyncio

from loguru import logger

from src.collectors.yahoo_finance import YahooFinanceCollector
from src.db import dispose_engine


async def main(days: int, symbols: list[str] | None) -> int:
    collector = YahooFinanceCollector(days=days, symbols=symbols)
    result = await collector.run()
    logger.info(
        f"backfill done: fetched={result.items_fetched} "
        f"new={result.items_new} errors={len(result.errors)}"
    )
    await dispose_engine()
    return 0 if not result.errors else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=365, help="Days of history to fetch")
    parser.add_argument("--symbols", nargs="*", help="Specific NSE symbols (defaults to all active)")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.days, args.symbols)))
