"""Angel One SmartAPI collector — REST auth + WebSocket tick stream."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

import pyotp
from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.collectors.base import BaseCollector, CollectorResult
from src.config import get_settings
from src.db import session_scope
from src.models.instrument import Instrument
from src.models.price import PriceTick
from src.models.watchlist import WatchlistItem

try:
    from SmartApi import SmartConnect  # type: ignore[import-not-found]
    from SmartApi.smartWebSocketV2 import SmartWebSocketV2  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — package name varies across versions
    SmartConnect = None  # type: ignore[assignment]
    SmartWebSocketV2 = None  # type: ignore[assignment]


class AngelOneCollector(BaseCollector):
    """Streams real-time ticks for all watchlist instruments.

    Operates as a long-running task, not a one-shot collector. Call `run_stream()`
    from the main entry point and await it; it will reconnect on failure.
    """

    job_name = "angel_one_ws"

    def __init__(self, source_id: str | None = None) -> None:
        super().__init__(source_id=source_id)
        self.settings = get_settings()
        self._sws: Any = None
        self._instruments_by_token: dict[str, Instrument] = {}
        self._running = False
        self._reconnect_delay = 5

    async def _collect(self) -> CollectorResult:
        """One-shot stub — Angel One is a streaming collector, not a poller."""
        result = CollectorResult()
        result.errors.append("AngelOneCollector is streaming; use run_stream() instead")
        return result

    async def authenticate(self) -> dict[str, Any]:
        """Generate session via SmartAPI login. Returns auth tokens."""
        if SmartConnect is None:
            raise RuntimeError("smartapi-python is not installed")
        s = self.settings
        if not all([s.angel_one_api_key, s.angel_one_client_id, s.angel_one_password, s.angel_one_totp_secret]):
            raise RuntimeError("Angel One credentials missing in .env")

        def _login() -> dict[str, Any]:
            totp = pyotp.TOTP(s.angel_one_totp_secret).now()
            client = SmartConnect(api_key=s.angel_one_api_key)
            data = client.generateSession(s.angel_one_client_id, s.angel_one_password, totp)
            feed_token = client.getfeedToken()
            return {
                "client": client,
                "jwt": data["data"]["jwtToken"],
                "refresh": data["data"]["refreshToken"],
                "feed_token": feed_token,
            }

        return await asyncio.to_thread(_login)

    async def _load_watchlist_tokens(self) -> list[dict[str, str]]:
        """Return list of {exchangeType, tokens} for instruments to stream.

        Pulls the union of:

        * ``WatchlistItem`` rows — user-tracked instruments.
        * Open ``Holding`` rows — anything the strategy is currently
          holding, so live ticks flow for positions the LLM opened that
          aren't on the watchlist. Without this, ``PriceService`` would
          serve stale ticks (or fall back to yfinance, which is slower
          and less reliable than the WebSocket) for active positions.
        """
        from src.models.portfolio import Holding
        async with session_scope() as session:
            watch_rows = await session.execute(
                select(Instrument)
                .join(WatchlistItem, WatchlistItem.instrument_id == Instrument.id)
                .where(Instrument.angel_one_token.is_not(None))
                .distinct()
            )
            watch = list(watch_rows.scalars())
            held_rows = await session.execute(
                select(Instrument)
                .join(Holding, Holding.instrument_id == Instrument.id)
                .where(Instrument.angel_one_token.is_not(None))
                .distinct()
            )
            held = list(held_rows.scalars())
        seen: set[uuid.UUID] = set()
        instruments: list[Instrument] = []
        for inst in watch + held:
            if inst.id in seen:
                continue
            seen.add(inst.id)
            instruments.append(inst)

        self._instruments_by_token = {i.angel_one_token: i for i in instruments if i.angel_one_token}

        by_exchange: dict[str, list[str]] = {}
        for inst in instruments:
            if not inst.angel_one_token:
                continue
            ex = "1" if inst.exchange == "NSE" else "3"
            by_exchange.setdefault(ex, []).append(inst.angel_one_token)

        return [{"exchangeType": int(ex), "tokens": toks} for ex, toks in by_exchange.items()]

    async def run_stream(self) -> None:
        """Run the WebSocket loop forever with exponential-backoff reconnects."""
        if SmartWebSocketV2 is None:
            logger.error("SmartWebSocketV2 not available — skipping Angel One stream")
            return

        self._running = True
        while self._running:
            try:
                auth = await self.authenticate()
                tokens = await self._load_watchlist_tokens()
                if not tokens:
                    logger.info("Angel One: no watchlist tokens mapped — sleeping 5m")
                    await asyncio.sleep(300)
                    continue

                await self._stream_loop(auth, tokens)
                self._reconnect_delay = 5  # reset on clean exit
            except Exception as exc:
                logger.exception(f"Angel One stream error: {exc}")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 300)

    async def _stream_loop(self, auth: dict[str, Any], tokens: list[dict[str, Any]]) -> None:
        """Open one WebSocket session, subscribe, and process ticks until disconnect."""
        loop = asyncio.get_running_loop()
        sws = SmartWebSocketV2(
            auth_token=auth["jwt"],
            api_key=self.settings.angel_one_api_key,
            client_code=self.settings.angel_one_client_id,
            feed_token=auth["feed_token"],
        )
        self._sws = sws
        disconnected = asyncio.Event()

        def on_data(wsapp: Any, message: Any) -> None:
            try:
                asyncio.run_coroutine_threadsafe(self._handle_tick(message), loop)
            except Exception as exc:
                logger.warning(f"Angel One tick handler: {exc}")

        def on_open(_: Any) -> None:
            logger.info("Angel One WS connected; subscribing")
            sws.subscribe("laabh", 1, tokens)  # mode 1 = LTP

        def on_error(_: Any, err: Any) -> None:
            logger.error(f"Angel One WS error: {err}")
            loop.call_soon_threadsafe(disconnected.set)

        def on_close(_: Any) -> None:
            logger.warning("Angel One WS closed")
            loop.call_soon_threadsafe(disconnected.set)

        sws.on_data = on_data
        sws.on_open = on_open
        sws.on_error = on_error
        sws.on_close = on_close

        await asyncio.to_thread(sws.connect)
        await disconnected.wait()

    async def _handle_tick(self, message: Any) -> None:
        """Persist a single tick from Angel One payload."""
        if not isinstance(message, dict):
            return
        token = str(message.get("token") or message.get("tk") or "")
        ltp_raw = message.get("last_traded_price") or message.get("ltp")
        if not token or ltp_raw is None:
            return
        inst = self._instruments_by_token.get(token)
        if not inst:
            return
        ltp = float(ltp_raw) / 100.0 if float(ltp_raw) > 100000 else float(ltp_raw)

        async with session_scope() as session:
            stmt = pg_insert(PriceTick).values(
                instrument_id=inst.id,
                timestamp=datetime.utcnow(),
                ltp=ltp,
                volume=message.get("volume_trade_for_the_day"),
            )
            stmt = stmt.on_conflict_do_nothing(index_elements=["instrument_id", "timestamp"])
            await session.execute(stmt)

    async def stop(self) -> None:
        """Stop the streaming loop."""
        self._running = False
        if self._sws is not None:
            try:
                await asyncio.to_thread(self._sws.close_connection)
            except Exception:
                pass
