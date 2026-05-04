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
from curl_cffi import requests as cf_requests
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
# NSE migrated to /api/option-chain-v3 in 2024–25. The legacy v1 endpoints
# (`/api/option-chain-indices`, `/api/option-chain-equities`) sit behind a
# stricter Akamai bot check that rejects non-browser TLS fingerprints and
# returns HTTP 200 with body `{}`. v3 works once we present a Chrome-impersonated
# TLS handshake (via curl_cffi) plus a properly warmed cookie jar.
_HOME_URL = f"{_NSE_BASE}/"
_WARMUP_URL = f"{_NSE_BASE}/option-chain"
_V3_URL = f"{_NSE_BASE}/api/option-chain-v3"

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

    def _build_headers(self, *, accept_html: bool = False) -> dict[str, str]:
        # Modern Chrome fingerprint — NSE anti-bot reads sec-ch-ua/sec-fetch-*.
        common = {
            "User-Agent": self._settings.nse_user_agent,
            "Accept-Language": "en-US,en;q=0.9,en-IN;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not.A/Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Sec-Fetch-Dest": "document" if accept_html else "empty",
            "Sec-Fetch-Mode": "navigate" if accept_html else "cors",
            "Sec-Fetch-Site": "none" if accept_html else "same-origin",
        }
        if accept_html:
            common["Accept"] = (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            )
        else:
            common["Accept"] = "application/json, text/plain, */*"
            common["Referer"] = _WARMUP_URL
        return common

    def _client_instance(self) -> "cf_requests.AsyncSession":
        """curl_cffi AsyncSession impersonates Chrome's TLS handshake exactly,
        which is required to defeat NSE's Akamai bot detection. The session
        also persists cookies across requests."""
        if self._client is None:
            self._client = cf_requests.AsyncSession(
                impersonate="chrome124",
                timeout=20.0,
            )
        return self._client

    def _cookies_stale(self) -> bool:
        interval = self._settings.nse_cookie_refresh_interval_min * 60
        return time.monotonic() - self._cookies_fetched_at > interval

    async def _refresh_cookies(self) -> None:
        """Two-step warmup: homepage then option-chain HTML page.

        Even with curl_cffi's Chrome TLS fingerprint, NSE expects a request
        sequence that mimics what a browser does: homepage first (sets the
        Akamai `_abck`, `bm_sz`, `ak_bmsc` cookies), then the option-chain
        HTML page (sets `nsit`, `nseappid` scoped to the API path).
        """
        client = self._client_instance()
        for url in (_HOME_URL, _WARMUP_URL):
            await _throttle(self._settings.nse_request_interval_sec)
            try:
                resp = await client.get(url, headers=self._build_headers(accept_html=True))
                if resp.status_code >= 400:
                    raise SourceUnavailableError(
                        f"NSE warmup {url} failed: HTTP {resp.status_code}"
                    )
            except Exception as exc:
                raise SourceUnavailableError(
                    f"NSE warmup network error on {url}: {exc}"
                ) from exc

        # Snapshot cookie names/count for logging only — curl_cffi's session
        # holds the real values on its internal jar.
        try:
            self._cookies = {c.name: c.value for c in client.cookies.jar}
        except Exception:
            self._cookies = dict(getattr(client.cookies, "items", lambda: {})() or {})
        self._cookies_fetched_at = time.monotonic()
        logger.debug(f"nse_source: cookies refreshed ({len(self._cookies)} cookies)")

    async def _ensure_cookies(self) -> None:
        if not self._cookies or self._cookies_stale():
            await self._refresh_cookies()

    # ------------------------------------------------------------------
    # HTTP call with one cookie-refresh retry
    # ------------------------------------------------------------------

    async def _get(self, url: str) -> Any:
        """Issue a GET with cookies; refresh once on auth failure or empty body."""
        for attempt in range(self._settings.nse_max_retries + 1):
            await _throttle(self._settings.nse_request_interval_sec)
            await self._ensure_cookies()
            client = self._client_instance()
            try:
                resp = await client.get(url, headers=self._build_headers())
            except Exception as exc:
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

            # NSE's anti-bot trick: HTTP 200 with body `{}`. Treat as a soft
            # auth failure and retry after refreshing the cookie jar.
            if isinstance(data, dict) and not data:
                if attempt < self._settings.nse_max_retries:
                    logger.warning(
                        f"nse_source: empty body — refreshing cookies (attempt {attempt + 1})"
                    )
                    self._cookies = {}
                    self._cookies_fetched_at = 0.0
                    continue
                raise SchemaError("NSE returned empty {} after retries", "{}")

            return data

        raise SourceUnavailableError("NSE: exhausted retries")

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _url_for(symbol: str, expiry_date: date) -> str:
        # v3 endpoint requires the type discriminator and a per-expiry filter.
        # Expiry format is `DD-MMM-YYYY` (e.g. `26-May-2026`).
        kind = "Indices" if symbol.upper() in _INDEX_SYMBOLS else "Equity"
        exp = expiry_date.strftime("%d-%b-%Y")
        sym = symbol.upper()
        from urllib.parse import quote
        return f"{_V3_URL}?type={kind}&symbol={quote(sym)}&expiry={quote(exp)}"

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

    @staticmethod
    def _parse_float(v: Any) -> float | None:
        try:
            return float(v) if v is not None and v != "" else None
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

        # v3 returns rows pre-filtered to the requested expiry, but the field
        # is now `expiryDates` (plural). v1 used `expiryDate`. Accept either.
        expiry_str = expiry_date.strftime("%d-%b-%Y")
        strikes: list[StrikeRow] = []

        for entry in data:
            if not isinstance(entry, dict):
                continue
            row_expiry = entry.get("expiryDates") or entry.get("expiryDate")
            if row_expiry and row_expiry != expiry_str:
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
                # v3 uses `buyPrice1`/`sellPrice1` for top-of-book; v1 used
                # `bidprice`/`askPrice`. Accept either.
                bid_v = opt_data.get("buyPrice1", opt_data.get("bidprice"))
                ask_v = opt_data.get("sellPrice1", opt_data.get("askPrice"))
                bid_qty_v = opt_data.get("buyQuantity1", opt_data.get("bidQty"))
                ask_qty_v = opt_data.get("sellQuantity1", opt_data.get("askQty"))
                strikes.append(
                    StrikeRow(
                        strike=strike,
                        option_type=opt_type,
                        ltp=self._parse_decimal(opt_data.get("lastPrice")),
                        bid=self._parse_decimal(bid_v),
                        ask=self._parse_decimal(ask_v),
                        bid_qty=self._parse_int(bid_qty_v),
                        ask_qty=self._parse_int(ask_qty_v),
                        volume=self._parse_int(opt_data.get("totalTradedVolume")),
                        oi=self._parse_int(opt_data.get("openInterest")),
                        # v3 ships IV; Greeks are computed downstream.
                        iv=self._parse_float(opt_data.get("impliedVolatility")),
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
        url = self._url_for(symbol, expiry_date)
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
        # curl_cffi AsyncSession has .close() (sync) — no aclose().
        if self._client is not None:
            try:
                close = getattr(self._client, "close", None)
                if callable(close):
                    res = close()
                    # In some versions close() is async
                    if hasattr(res, "__await__"):
                        await res
            except Exception:
                pass
            self._client = None
