"""Quick sanity check: mint a Dhan access token via TOTP and print metadata.

Run: ``python scripts/test_dhan_totp.py``
Requires DHAN_CLIENT_ID, DHAN_PIN, DHAN_TOTP_SECRET in .env.
"""
from __future__ import annotations

import asyncio
import sys

from loguru import logger

from src.auth.dhan_token import DhanAuthError, manager


async def main() -> int:
    try:
        token = await manager.get_access_token()
    except DhanAuthError as exc:
        logger.error(f"Dhan auth failed: {exc}")
        return 1
    except Exception as exc:
        logger.exception(f"Unexpected error: {exc}")
        return 1

    logger.info("Authenticated with Dhan successfully")
    logger.info(f"Client: {token.client_name} ({token.client_id})")
    logger.info(f"Token (truncated): {token.access_token[:40]}...")
    logger.info(f"Expires (UTC): {token.expiry.isoformat()}")

    # Second call should be served from cache (no second HTTP login).
    cached = await manager.get_access_token()
    if cached.access_token != token.access_token:
        logger.error("Cache returned a different token — manager is broken")
        return 1
    logger.info("Cache hit on second call — manager is working as expected")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
