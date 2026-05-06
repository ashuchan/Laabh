"""Dhan API access-token minter (TOTP-based programmatic login).

Endpoint: ``POST https://auth.dhan.co/app/generateAccessToken``
Spec: https://dhanhq.co/docs/v2/authentication/

Single responsibility: turn (client_id, pin, totp_secret) into a usable
24h access token, with an in-process cache so repeated callers don't burn
through Dhan's per-day token-mint quota (~25/day).

Consumers ask ``manager.get_access_token()`` and trust the cache; near-expiry
the next call automatically mints a fresh token. On a 401 from a downstream
Dhan call (token revoked server-side mid-life), callers should re-fetch with
``force_refresh=True``.

Note on CLAUDE.md ``as_of`` / ``dryrun_run_id`` convention: this module is
cross-cutting auth infrastructure, not a pipeline step. Time-of-day and
dry-run identity are irrelevant to token minting, so the convention is
deliberately not applied here.

Async-only: all I/O entry points are coroutines. ``DhanTokenManager`` is
async-safe (asyncio.Lock) but **not** thread-safe — do not call from threads.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.config import get_settings
from src.utils.totp import generate_totp

_AUTH_URL = "https://auth.dhan.co/app/generateAccessToken"
# Refresh slightly before the documented 24h ceiling so a request inflight at
# the rollover boundary never lands on a 401.
_REFRESH_BUFFER = timedelta(minutes=10)
# Treat tokens as valid for 23h regardless of what the server's expiryTime
# field says — the field is naive (no tz) and we'd rather be conservative
# than mis-interpret IST vs UTC and serve a stale token.
_TOKEN_LIFETIME = timedelta(hours=23)


class DhanAuthError(RuntimeError):
    """Raised when Dhan's auth endpoint rejects credentials or is unreachable."""


class _TransientDhanAuthError(DhanAuthError):
    """Internal: 5xx from auth endpoint. Distinct subtype so tenacity retries
    these but not 4xx (bad credentials), which should fail fast.
    """


@dataclass(frozen=True)
class DhanAccessToken:
    """Immutable bundle returned by ``generateAccessToken``."""

    access_token: str
    client_id: str
    client_name: str
    expiry: datetime  # UTC

    def is_expired(self, *, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return now + _REFRESH_BUFFER >= self.expiry


class DhanTokenManager:
    """Process-wide cache + minter for Dhan access tokens.

    Concurrent callers de-duplicate via an asyncio lock, so only one HTTP
    login fires at a time even under burst load. Async-only — not thread-safe.
    """

    def __init__(self) -> None:
        self._token: DhanAccessToken | None = None
        self._lock = asyncio.Lock()

    async def get_access_token(self, *, force_refresh: bool = False) -> DhanAccessToken:
        if not force_refresh and self._token and not self._token.is_expired():
            return self._token
        async with self._lock:
            if not force_refresh and self._token and not self._token.is_expired():
                return self._token
            self._token = await self._mint()
            logger.info(
                f"dhan_auth: minted token for {self._token.client_id}, "
                f"expires {self._token.expiry.isoformat()}"
            )
            return self._token

    def reset(self) -> None:
        """Drop the cached token. Tests use this to ensure isolation."""
        self._token = None

    @retry(
        retry=retry_if_exception_type(
            (
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.RemoteProtocolError,
                _TransientDhanAuthError,
            )
        ),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def _mint(self) -> DhanAccessToken:
        s = get_settings()
        if not (s.dhan_client_id and s.dhan_pin and s.dhan_totp_secret):
            raise DhanAuthError(
                "DHAN_CLIENT_ID, DHAN_PIN, and DHAN_TOTP_SECRET must all be set in .env"
            )
        code = generate_totp(s.dhan_totp_secret)
        params = {
            "dhanClientId": s.dhan_client_id,
            "pin": s.dhan_pin,
            "totp": code,
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(_AUTH_URL, params=params)
        if resp.status_code >= 500:
            # Server-side blip — let tenacity retry.
            raise _TransientDhanAuthError(
                f"Dhan auth 5xx {resp.status_code}: {resp.text[:200]}"
            )
        if resp.status_code != 200:
            # 4xx — credentials are wrong or TOTP window slipped. Fail fast.
            raise DhanAuthError(
                f"Dhan auth returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        body: dict[str, Any] = resp.json()
        token = body.get("accessToken")
        if not token:
            raise DhanAuthError(f"No accessToken in response: {body}")
        return DhanAccessToken(
            access_token=token,
            client_id=body.get("dhanClientId", s.dhan_client_id),
            client_name=body.get("dhanClientName", ""),
            expiry=datetime.now(timezone.utc) + _TOKEN_LIFETIME,
        )


# Module-level singleton — callers (DhanSource, smoke-test script) import this.
manager = DhanTokenManager()


async def get_dhan_headers(*, force_refresh: bool = False) -> dict[str, str]:
    """Return the auth header dict for any Dhan API call.

    Two-tier resolution, in priority order:
      1. If DHAN_PIN and DHAN_TOTP_SECRET are set → mint/reuse a token via
         :class:`DhanTokenManager` (preferred — auto-rotates every 24h).
      2. Else, if DHAN_ACCESS_TOKEN is set → use it as-is (legacy manual mode).

    Raises :class:`DhanAuthError` if neither path is configured. This is the
    one place that owns header construction; callers in ``dhan_source`` and
    ``dhan_historical`` thin-wrap this and translate the exception type.

    ``force_refresh`` triggers a fresh mint on the manager path; on the
    static-token path it has no effect (there is nothing to refresh).
    """
    s = get_settings()
    if not s.dhan_client_id:
        raise DhanAuthError("DHAN_CLIENT_ID is not configured")
    if s.dhan_pin and s.dhan_totp_secret:
        token = await manager.get_access_token(force_refresh=force_refresh)
        return {
            "access-token": token.access_token,
            "client-id": token.client_id,
            "Content-Type": "application/json",
        }
    if s.dhan_access_token:
        return {
            "access-token": s.dhan_access_token,
            "client-id": s.dhan_client_id,
            "Content-Type": "application/json",
        }
    raise DhanAuthError(
        "Dhan auth not configured: set DHAN_PIN+DHAN_TOTP_SECRET (preferred) "
        "or DHAN_ACCESS_TOKEN in .env"
    )
