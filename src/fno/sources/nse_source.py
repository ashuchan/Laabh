"""NSE option chain source adapter.

Session warmup: GET /option-chain first to receive cookies, then API requests
with those cookies.  Cookies are cached in-process and refreshed on schedule
or on auth failure.

URL routing:
  Indices  → /api/option-chain-indices?symbol=NIFTY
  Equities → /api/option-chain-equities?symbol=RELIANCE
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

_NSE_BASE = "https://www.nseindia.com"
_WARMUP_URL = f"{_NSE_BASE}/option-chain"
_INDICES_URL = f"{_NSE_BASE}/api/option-chain-indices"
_EQUITIES_URL = f"{_NSE_BASE}/api/option-chain-equities"

# Symbols treated as indices (use /api/option-chain-indices)
_INDEX_SYMBOLS = frozenset(
    {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
)

# Global semaphore — ensures minimum interval between any two NSE calls
_nse_semaphore: asyncio.Semaphore = asyncio.Semaphore(1)
_last_call_ts: float = 0.0


async def _throttle(interval_sec: float) -> None:
    """Enforce a minimum interval between NSE HTTP calls system-wide."""
    global _last_call_ts
    async with _nse_semaphore:
        now = time.monotonic()
        wait = interval_sec - (now - _last_call_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call_ts = time.monotonic()


class NSESource(BaseChainSource):
    """Fetches option chain data from NSE's public JSON API."""

    name: ClassVar[str] = "nse"

    def __init__(self) -> None:
        self._settings = get_settings()
        self._cookies: dict[str, str] = {}
        self._cookies_fetched_at: float = 0.0
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _build_headers(self) -> dict[str, str]:
        return {
            "User-Agent": self._settings.nse_user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": _WARMUP_URL,
            "Connection": "keep-alive",
        }

    def _client_instance(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(15.0),
                follow_redirects=True,
            )
        return self._client

    def _cookies_stale(self) -> bool:
        interval = self._settings.nse_cookie_refresh_interval_min * 60
        return time.monotonic() - self._cookies_fetched_at > interval

    async def _refresh_cookies(self) -> None:
        """GET the NSE option-chain page to obtain fresh session cookies."""
        await _throttle(self._settings.nse_request_interval_sec)
        client = self._client_instance()
        try:
            resp = await client.get(_WARMUP_URL, headers=self._build_headers())
            resp.raise_for_status()
            self._cookies = dict(resp.cookies)
            self._cookies_fetched_at = time.monotonic()
            logger.debug("nse_source: cookies refreshed")
        except httpx.HTTPStatusError as exc:
            raise SourceUnavailableError(
                f"NSE cookie warmup failed: {exc.response.status_code}"
            ) from exc
        except httpx.RequestError as exc:
            raise SourceUnavailableError(f"NSE cookie warmup network error: {exc}") from exc

    async def _ensure_cookies(self) -> None:
        if not self._cookies or self._cookies_stale():
            await self._refresh_cookies()

    # ------------------------------------------------------------------
    # HTTP call with one cookie-refresh retry
    # ------------------------------------------------------------------

    async def _get(self, url: str) -> Any:
        """Issue a GET with cookies; refresh once on auth failure."""
        for attempt in range(self._settings.nse_max_retries + 1):
            await _throttle(self._settings.nse_request_interval_sec)
            await self._ensure_cookies()
            client = self._client_instance()
            try:
                resp = await client.get(
                    url,
                    headers=self._build_headers(),
                    cookies=self._cookies,
                )
            except httpx.RequestError as exc:
                raise SourceUnavailableError(f"NSE network error: {exc}") from exc

            if resp.status_code == 401 or resp.status_code == 403:
                if attempt < self._settings.nse_max_retries:
                    logger.warning(
                        f"nse_source: HTTP {resp.status_code} — refreshing cookies (attempt {attempt + 1})"
                    )
                    self._cookies = {}
                    self._cookies_fetched_at = 0.0
                    continue
                raise AuthError(f"NSE returned {resp.status_code} after cookie refresh")

            if resp.status_code == 429:
                raise RateLimitError("NSE rate-limited us")

            if resp.status_code >= 500:
                raise SourceUnavailableError(f"NSE server error {resp.status_code}")

            if resp.status_code != 200:
                raise SourceUnavailableError(f"NSE unexpected status {resp.status_code}")

            try:
                data = resp.json()
            except Exception as exc:
                raw = resp.text[:8192]
                raise SchemaError(f"NSE response is not valid JSON: {exc}", raw) from exc

            return data

        raise SourceUnavailableError("NSE: exhausted retries")

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _url_for(symbol: str) -> str:
        if symbol.upper() in _INDEX_SYMBOLS:
            return f"{_INDICES_URL}?symbol={symbol.upper()}"
        return f"{_EQUITIES_URL}?symbol={symbol.upper()}"

    @staticmethod
    def _parse_decimal(v: Any) -> Decimal | None:
        try:
            return Decimal(str(v)) if v is not None else None
        except Exception:
            return None

    @staticmethod
    def _parse_int(v: Any) -> int | None:
        try:
            return int(v) if v is not None else None
        except Exception:
            return None

    def _parse_response(
        self, raw: Any, symbol: str, expiry_date: date
    ) -> ChainSnapshot:
        """Convert NSE JSON payload to ChainSnapshot, raising SchemaError on mismatch."""
        raw_str = str(raw)[:8192]

        if not isinstance(raw, dict):
            raise SchemaError("NSE response root is not a dict", raw_str)

        records = raw.get("records")
        if not isinstance(records, dict):
            raise SchemaError("NSE response missing 'records' dict", raw_str)

        data = records.get("data")
        if not isinstance(data, list):
            raise SchemaError("NSE records.data is not a list", raw_str)

        underlying_ltp = self._parse_decimal(records.get("underlyingValue"))
        snapshot_at = datetime.now(tz=timezone.utc)

        # NSE returns all expiries; filter to the requested one
        expiry_str = expiry_date.strftime("%d-%b-%Y")
        strikes: list[StrikeRow] = []

        for entry in data:
            if not isinstance(entry, dict):
                continue
            if entry.get("expiryDate") != expiry_str:
                continue
            strike_val = entry.get("strikePrice")
            if strike_val is None:
                continue
            strike = self._parse_decimal(strike_val)
            if strike is None:
                continue

            for opt_type, key in (("CE", "CE"), ("PE", "PE")):
                opt_data = entry.get(key)
                if not isinstance(opt_data, dict):
                    continue
                strikes.append(
                    StrikeRow(
                        strike=strike,
                        option_type=opt_type,
                        ltp=self._parse_decimal(opt_data.get("lastPrice")),
                        bid=self._parse_decimal(opt_data.get("bidprice")),
                        ask=self._parse_decimal(opt_data.get("askPrice")),
                        bid_qty=self._parse_int(opt_data.get("bidQty")),
                        ask_qty=self._parse_int(opt_data.get("askQty")),
                        volume=self._parse_int(opt_data.get("totalTradedVolume")),
                        oi=self._parse_int(opt_data.get("openInterest")),
                        iv=None,  # NSE does not supply Greeks — parser computes them
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
        """Fetch and parse the NSE option chain for (symbol, expiry_date)."""
        url = self._url_for(symbol)
        raw = await self._get(url)
        snapshot = self._parse_response(raw, symbol, expiry_date)
        if not snapshot.strikes:
            raise SourceUnavailableError(
                f"NSE returned empty chain for {symbol} expiry {expiry_date}"
            )
        logger.info(
            f"nse_source: {symbol} {expiry_date} → {len(snapshot.strikes)} strikes"
        )
        return snapshot

    async def health_check(self) -> bool:
        """Lightweight probe — just warm the cookies."""
        try:
            await self._refresh_cookies()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
