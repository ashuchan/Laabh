"""India VIX collector — fetches live VIX from Angel One and classifies regime."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import get_settings
from src.db import session_scope
from src.models.fno_vix import VIXTick

# Angel One instrument token for INDIA VIX
_VIX_TOKEN = "26017"
_VIX_SYMBOL = "INDIA VIX"


def classify_regime(vix_value: float) -> str:
    """Return VIX regime: 'low' | 'neutral' | 'high'."""
    settings = get_settings()
    if vix_value < settings.fno_vix_low_threshold:
        return "low"
    if vix_value <= settings.fno_vix_high_threshold:
        return "neutral"
    return "high"


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=2, max=30))
async def _fetch_vix_from_angel_one() -> float:
    """Fetch current India VIX value via Angel One REST API."""
    import pyotp
    from SmartApi import SmartConnect  # type: ignore[import]

    settings = get_settings()
    if not settings.angel_one_api_key:
        raise RuntimeError("ANGEL_ONE_API_KEY not configured")

    totp = pyotp.TOTP(settings.angel_one_totp_secret).now()
    obj = SmartConnect(api_key=settings.angel_one_api_key)
    data = obj.generateSession(
        settings.angel_one_client_id,
        settings.angel_one_password,
        totp,
    )
    if data.get("status") is False:
        raise RuntimeError(f"Angel One auth failed: {data.get('message')}")

    ltp_data = obj.ltpData("NSE", _VIX_SYMBOL, _VIX_TOKEN)
    if ltp_data.get("status") is False:
        raise RuntimeError(f"VIX ltp failed: {ltp_data.get('message')}")

    return float(ltp_data["data"]["ltp"])


async def _fetch_vix_historical(as_of: datetime) -> float:
    """Fetch India VIX from yfinance for a historical timestamp."""
    import asyncio

    def _sync_fetch() -> float:
        import yfinance as yf
        start = (as_of - timedelta(days=3)).date()
        end = (as_of + timedelta(days=1)).date()
        hist = yf.Ticker("^INDIAVIX").history(start=str(start), end=str(end))
        if hist.empty:
            raise RuntimeError(f"No VIX history from yfinance for {as_of.date()}")
        # Pick the row closest to as_of
        hist.index = hist.index.tz_localize("Asia/Kolkata") if hist.index.tzinfo is None else hist.index.tz_convert("Asia/Kolkata")
        as_of_ist = as_of.astimezone(__import__("pytz").timezone("Asia/Kolkata"))
        candidates = hist[hist.index <= as_of_ist]
        if candidates.empty:
            return float(hist["Close"].iloc[0])
        return float(candidates["Close"].iloc[-1])

    return await asyncio.get_running_loop().run_in_executor(None, _sync_fetch)


async def run_once(
    vix_override: float | None = None,
    *,
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> VIXTick:
    """Fetch VIX, classify regime, persist to vix_ticks, and return the row.

    Pass `vix_override` in tests to skip the live API call.
    When `as_of` is set, fetches historical VIX from yfinance at that timestamp.
    """
    if vix_override is not None:
        vix_value = vix_override
    elif as_of is not None:
        vix_value = await _fetch_vix_historical(as_of)
    else:
        vix_value = await _fetch_vix_from_angel_one()

    regime = classify_regime(vix_value)
    stamp = as_of if as_of is not None else datetime.now(tz=timezone.utc)

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    async with session_scope() as session:
        stmt = pg_insert(VIXTick).values(
            timestamp=stamp,
            vix_value=vix_value,
            regime=regime,
            dryrun_run_id=dryrun_run_id,
        ).on_conflict_do_nothing(index_elements=["timestamp"])
        await session.execute(stmt)

    row = VIXTick(
        timestamp=stamp,
        vix_value=vix_value,
        regime=regime,
        dryrun_run_id=dryrun_run_id,
    )
    logger.info(f"vix_collector: VIX={vix_value:.2f} regime={regime}")
    return row


async def latest_vix() -> VIXTick | None:
    """Return the most recent VIX tick, or None if no data yet."""
    from sqlalchemy import select

    async with session_scope() as session:
        result = await session.execute(
            select(VIXTick).order_by(VIXTick.timestamp.desc()).limit(1)
        )
        return result.scalar_one_or_none()
