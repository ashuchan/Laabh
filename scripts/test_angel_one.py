"""Quick sanity check: authenticate with Angel One and print the feed token."""
from __future__ import annotations

import asyncio
import sys

from loguru import logger

from src.collectors.angel_one import AngelOneCollector


async def main() -> int:
    collector = AngelOneCollector()
    try:
        auth = await collector.authenticate()
    except Exception as exc:
        logger.error(f"Authentication failed: {exc}")
        return 1

    logger.info("Authenticated with Angel One successfully")
    logger.info(f"JWT (truncated): {auth['jwt'][:40]}...")
    logger.info(f"Feed token (truncated): {auth['feed_token'][:40]}...")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
