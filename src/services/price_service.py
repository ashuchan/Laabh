"""Price data queries and watchlist alert checking."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from loguru import logger
from pytz import timezone as tz
from sqlalchemy import desc, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.config import get_settings
from src.db import session_scope
from src.models.instrument import Instrument
from src.models.price import PriceDaily, PriceTick
from src.models.watchlist import WatchlistItem
from src.services.notification_service import NotificationService

# A tick older than this is considered stale during market hours and
# triggers a live re-fetch. Five minutes is short enough that the digest
# and the LLM see near-current prices, long enough that we don't hammer
# yfinance on every call inside a strategist run.
_STALE_TICK_MINUTES = 5


def _ist_now() -> datetime:
    settings = get_settings()
    return datetime.now(tz=tz(settings.timezone))


def _is_market_hours(now: datetime | None = None) -> bool:
    """Mon–Fri, between MARKET_OPEN_TIME and MARKET_CLOSE_TIME (IST)."""
    settings = get_settings()
    now = now or _ist_now()
    if now.weekday() >= 5:
        return False
    try:
        oh, om = (int(p) for p in settings.market_open_time.split(":"))
        ch, cm = (int(p) for p in settings.market_close_time.split(":"))
    except Exception:
        oh, om, ch, cm = 9, 15, 15, 30
    open_t = now.replace(hour=oh, minute=om, second=0, microsecond=0)
    close_t = now.replace(hour=ch, minute=cm, second=0, microsecond=0)
    return open_t <= now <= close_t


def _tick_age_minutes(ts: datetime) -> float:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 60.0


class PriceService:
    """Query latest price and evaluate price-alert thresholds."""

    def __init__(self, notifier: NotificationService | None = None) -> None:
        self.notifier = notifier or NotificationService()

    async def latest_price(self, instrument_id: uuid.UUID) -> float | None:
        """Return the most recent price, refreshing from yfinance if stale.

        Behaviour:

        1. Look up the latest ``PriceTick``. If it exists *and* either the
           market is closed or the tick is younger than
           ``_STALE_TICK_MINUTES``, return it.
        2. Otherwise, during market hours, fetch a live LTP from yfinance
           and persist it as a fresh ``PriceTick`` so subsequent callers
           hit the cache.
        3. As a final fallback, return ``price_daily.close``. Outside
           market hours this is the right answer; inside, it means both
           the WebSocket and yfinance failed and the caller should treat
           the value as stale.

        Without this, the strategist and the intraday digest were both
        reading whatever stale tick happened to be in the table — which
        for non-watchlist holdings could be days old, since the Angel One
        WebSocket only subscribes to ``WatchlistItem`` rows, not arbitrary
        positions the LLM has opened.
        """
        async with session_scope() as session:
            row = await session.execute(
                select(PriceTick.ltp, PriceTick.timestamp)
                .where(PriceTick.instrument_id == instrument_id)
                .order_by(desc(PriceTick.timestamp))
                .limit(1)
            )
            tick_row = row.first()

        in_market = _is_market_hours()
        if tick_row is not None:
            ltp_val, ts = float(tick_row[0]), tick_row[1]
            if not in_market or _tick_age_minutes(ts) <= _STALE_TICK_MINUTES:
                return ltp_val

        if in_market:
            live = await self._fetch_live_yf(instrument_id)
            if live is not None:
                return live
            # Live fetch failed — better to return the stale tick we had
            # than to drop all the way to yesterday's close, which would
            # be even less accurate during a live session.
            if tick_row is not None:
                logger.debug(
                    f"price_service: stale tick returned for {instrument_id} "
                    f"(yfinance refresh failed)"
                )
                return float(tick_row[0])

        async with session_scope() as session:
            row = await session.execute(
                select(PriceDaily.close)
                .where(PriceDaily.instrument_id == instrument_id)
                .order_by(desc(PriceDaily.date))
                .limit(1)
            )
            val = row.scalar_one_or_none()
            return float(val) if val is not None else None

    async def _fetch_live_yf(self, instrument_id: uuid.UUID) -> float | None:
        """Fetch a live LTP via yfinance and persist as a PriceTick.

        Uses ``Ticker.history(period='1d', interval='1m')`` so the result
        is intraday-current during market hours. Persisting the tick lets
        a follow-up ``latest_price`` call within ``_STALE_TICK_MINUTES``
        re-use it instead of hitting yfinance again — important because a
        single strategist run can trigger many price lookups.
        """
        async with session_scope() as session:
            inst = await session.get(Instrument, instrument_id)
            yahoo_symbol = inst.yahoo_symbol if inst else None
        if not yahoo_symbol:
            logger.debug(
                f"price_service: no yahoo_symbol for {instrument_id} — "
                f"cannot live-refresh"
            )
            return None

        try:
            import yfinance as yf
        except ImportError:
            logger.warning("yfinance not available; install to enable live refresh")
            return None

        def _fetch() -> float | None:
            try:
                t = yf.Ticker(yahoo_symbol)
                df = t.history(period="1d", interval="1m")
                if df is None or df.empty:
                    return None
                close_series = df["Close"]
                if hasattr(close_series, "iloc"):
                    val = close_series.iloc[-1]
                else:
                    val = close_series[-1]
                if val is None or val != val:  # NaN check
                    return None
                return float(val)
            except Exception as exc:
                logger.warning(f"yfinance live {yahoo_symbol}: {exc}")
                return None

        ltp = await asyncio.to_thread(_fetch)
        if ltp is None:
            return None

        try:
            async with session_scope() as session:
                stmt = pg_insert(PriceTick).values(
                    instrument_id=instrument_id,
                    timestamp=datetime.utcnow(),
                    ltp=ltp,
                )
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["instrument_id", "timestamp"]
                )
                await session.execute(stmt)
        except Exception as exc:
            logger.warning(f"price_service: persist live tick failed: {exc!r}")
        return ltp

    async def check_price_alerts(self) -> int:
        """Check all watchlist price alerts against latest tick; notify if crossed."""
        sql = text("""
            WITH latest AS (
                SELECT DISTINCT ON (instrument_id) instrument_id, ltp
                FROM price_ticks
                WHERE timestamp > NOW() - INTERVAL '15 minutes'
                ORDER BY instrument_id, timestamp DESC
            )
            SELECT wi.id AS wi_id, wi.instrument_id, wi.price_alert_above, wi.price_alert_below,
                   i.symbol, i.company_name, l.ltp
            FROM watchlist_items wi
            JOIN instruments i ON i.id = wi.instrument_id
            JOIN latest l ON l.instrument_id = wi.instrument_id
            WHERE (wi.price_alert_above IS NOT NULL AND l.ltp >= wi.price_alert_above)
               OR (wi.price_alert_below IS NOT NULL AND l.ltp <= wi.price_alert_below)
        """)
        count = 0
        async with session_scope() as session:
            rows = await session.execute(sql)
            triggered = list(rows.mappings())

        for row in triggered:
            ltp = float(row["ltp"])
            if row["price_alert_above"] is not None and ltp >= float(row["price_alert_above"]):
                direction = f"crossed above ₹{row['price_alert_above']}"
            else:
                direction = f"dropped below ₹{row['price_alert_below']}"
            await self.notifier.create(
                type_="price_alert",
                title=f"{row['symbol']} @ ₹{ltp:.2f}",
                body=f"{row['company_name']} {direction}",
                priority="high",
                instrument_id=row["instrument_id"],
            )
            count += 1
        logger.info(f"price alerts triggered: {count}")
        return count
