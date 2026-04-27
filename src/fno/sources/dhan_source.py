"""Dhan option chain source adapter.

Endpoint: POST https://api.dhan.co/v2/optionchain
Auth: access-token + client-id headers.
Rate: 1 req per DHAN_REQUEST_INTERVAL_SEC *per underlying* (token bucket per symbol).
Dhan returns Greeks natively — the parser passes them through unchanged.
"""
from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, ClassVar

import httpx
from loguru import logger

from src.config import get_settings
from src.fno.sources.base import BaseChainSource, ChainSnapshot, StrikeRow
from src.fno.sources.exceptions import (
    AuthError,
    RateLimitError,
    SchemaError,
    SourceUnavailableError,
)

_DHAN_CHAIN_URL = "https://api.dhan.co/v2/optionchain"
_DHAN_HEALTH_URL = "https://api.dhan.co/v2/marketstatus"

# Dhan segment codes
_SEG_INDEX = "IDX_I"
_SEG_EQUITY = "NSE_FNO"
_INDEX_SYMBOLS = frozenset(
    {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
)


class DhanSource(BaseChainSource):
    """Fetches option chain data from Dhan's v2 API."""

    name: ClassVar[str] = "dhan"

    def __init__(self) -> None:
        self._settings = get_settings()
        # Per-underlying token bucket: maps symbol → last call timestamp
        self._last_call: dict[str, float] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        token = self._settings.dhan_access_token
        client_id = self._settings.dhan_client_id
        if not token or not client_id:
            raise AuthError("DHAN_ACCESS_TOKEN or DHAN_CLIENT_ID not configured")
        return {
            "access-token": token,
            "client-id": client_id,
            "Content-Type": "application/json",
        }

    def _client_instance(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        return self._client

    async def _throttle_for(self, symbol: str) -> None:
        """Per-symbol rate limiting — serialise same-symbol calls while allowing parallel different-symbol calls."""
        interval = self._settings.dhan_request_interval_sec
        async with self._lock:
            last = self._last_call.get(symbol, 0.0)
            wait = interval - (time.monotonic() - last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call[symbol] = time.monotonic()

    @staticmethod
    def _segment_for(symbol: str) -> str:
        return _SEG_INDEX if symbol.upper() in _INDEX_SYMBOLS else _SEG_EQUITY

    @staticmethod
    def _parse_decimal(v: Any) -> Decimal | None:
        try:
            return Decimal(str(v)) if v is not None else None
        except Exception:
            return None

    @staticmethod
    def _parse_float(v: Any) -> float | None:
        try:
            return float(v) if v is not None else None
        except Exception:
            return None

    @staticmethod
    def _parse_int(v: Any) -> int | None:
        try:
            return int(v) if v is not None else None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_response(
        self, raw: Any, symbol: str, expiry_date: date
    ) -> ChainSnapshot:
        raw_str = str(raw)[:8192]

        if not isinstance(raw, dict):
            raise SchemaError("Dhan response root is not a dict", raw_str)

        # Dhan v2 wraps data under 'data'
        data = raw.get("data")
        if not isinstance(data, dict):
            raise SchemaError("Dhan response missing 'data' dict", raw_str)

        underlying_ltp = self._parse_decimal(data.get("last_price"))

        strike_data = data.get("oc")
        if not isinstance(strike_data, dict):
            raise SchemaError("Dhan response 'data.oc' is not a dict", raw_str)

        snapshot_at = datetime.now(tz=timezone.utc)
        strikes: list[StrikeRow] = []

        for strike_str, opts in strike_data.items():
            if not isinstance(opts, dict):
                continue
            try:
                strike = Decimal(strike_str)
            except Exception:
                continue

            for opt_type, key in (("CE", "call"), ("PE", "put")):
                opt_data = opts.get(key)
                if not isinstance(opt_data, dict):
                    continue
                strikes.append(
                    StrikeRow(
                        strike=strike,
                        option_type=opt_type,
                        ltp=self._parse_decimal(opt_data.get("last_price")),
                        bid=self._parse_decimal(opt_data.get("bid_price")),
                        ask=self._parse_decimal(opt_data.get("ask_price")),
                        bid_qty=self._parse_int(opt_data.get("bid_qty")),
                        ask_qty=self._parse_int(opt_data.get("ask_qty")),
                        volume=self._parse_int(opt_data.get("volume")),
                        oi=self._parse_int(opt_data.get("oi")),
                        # Dhan provides Greeks natively
                        iv=self._parse_float(opt_data.get("implied_volatility")),
                        delta=self._parse_float(opt_data.get("delta")),
                        gamma=self._parse_float(opt_data.get("gamma")),
                        theta=self._parse_float(opt_data.get("theta")),
                        vega=self._parse_float(opt_data.get("vega")),
                    )
                )

        return ChainSnapshot(
            symbol=symbol,
            expiry_date=expiry_date,
            underlying_ltp=underlying_ltp,
            snapshot_at=snapshot_at,
            strikes=strikes,
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def fetch(self, symbol: str, expiry_date: date) -> ChainSnapshot:
        """Fetch and parse the Dhan option chain for (symbol, expiry_date)."""
        await self._throttle_for(symbol)

        headers = self._headers()  # raises AuthError if not configured
        payload = {
            "UnderlyingScrip": symbol.upper(),
            "UnderlyingSeg": self._segment_for(symbol),
            "Expiry": expiry_date.strftime("%Y-%m-%d"),
        }

        client = self._client_instance()
        try:
            resp = await client.post(_DHAN_CHAIN_URL, headers=headers, json=payload)
        except httpx.RequestError as exc:
            raise SourceUnavailableError(f"Dhan network error: {exc}") from exc

        if resp.status_code in (401, 403):
            raise AuthError(f"Dhan returned {resp.status_code} — check credentials")
        if resp.status_code == 429:
            raise RateLimitError("Dhan rate-limited us")
        if resp.status_code >= 500:
            raise SourceUnavailableError(f"Dhan server error {resp.status_code}")
        if resp.status_code != 200:
            raise SourceUnavailableError(f"Dhan unexpected status {resp.status_code}")

        try:
            raw = resp.json()
        except Exception as exc:
            raw_text = resp.text[:8192]
            raise SchemaError(f"Dhan response is not valid JSON: {exc}", raw_text) from exc

        snapshot = self._parse_response(raw, symbol, expiry_date)
        if not snapshot.strikes:
            raise SourceUnavailableError(
                f"Dhan returned empty chain for {symbol} expiry {expiry_date}"
            )

        logger.info(
            f"dhan_source: {symbol} {expiry_date} → {len(snapshot.strikes)} strikes"
        )
        return snapshot

    async def health_check(self) -> bool:
        """Lightweight probe — hit the Dhan market status endpoint."""
        try:
            headers = self._headers()
            client = self._client_instance()
            resp = await client.get(_DHAN_HEALTH_URL, headers=headers)
            return resp.status_code < 400
        except Exception:
            return False

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
