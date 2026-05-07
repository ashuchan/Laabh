"""External-service connectivity checks for preflight."""
from __future__ import annotations

import os
import time
from typing import Any

import httpx
from loguru import logger
from sqlalchemy import text

from src.auth.dhan_token import DhanAuthError, get_dhan_headers
from src.db import get_engine
from src.runday.checks.base import CheckResult, Severity
from src.runday.config import RundaySettings

# Env-var requirements per connectivity check. The value is a list of *groups*;
# at least ONE group must be fully present (any-of), and within a group all
# vars must be set (all-of). For checks with a single fixed requirement set,
# the outer list has one entry. Dhan supports two auth modes — a manual access
# token, or programmatic PIN+TOTP minting — so it carries two groups.
_ENV_VARS_BY_CHECK: dict[str, list[list[str]]] = {
    "preflight.db_connectivity": [["DATABASE_URL"]],
    "preflight.anthropic": [["ANTHROPIC_API_KEY"]],
    "preflight.telegram": [["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]],
    "preflight.angel_one": [[
        "ANGEL_ONE_API_KEY",
        "ANGEL_ONE_CLIENT_ID",
        "ANGEL_ONE_PASSWORD",
        "ANGEL_ONE_TOTP_SECRET",
    ]],
    "preflight.dhan": [
        ["DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN"],
        ["DHAN_CLIENT_ID", "DHAN_PIN", "DHAN_TOTP_SECRET"],
    ],
    # GITHUB_TOKEN is optional — issue_filer.run() degrades gracefully when
    # missing (logs a WARN and skips). GitHubCheck still surfaces a WARN if
    # the token is absent so the operator sees the state without preflight
    # blocking the trading day.
}


class EnvCheck:
    """Verify required env vars are present, excluding those for skipped checks."""

    name = "preflight.env"

    def __init__(
        self,
        settings: RundaySettings,
        skipped_checks: set[str] | None = None,
    ) -> None:
        self._settings = settings
        self._skipped = skipped_checks or set()

    async def run(self) -> CheckResult:
        # Aggregate "missing" across checks; for any-of groups we report the
        # group with the fewest unmet vars so the operator sees the closest
        # path to passing rather than a union of all alternatives.
        union_missing: list[str] = []
        seen: set[str] = set()
        total_groups_evaluated = 0

        for check_name, groups in _ENV_VARS_BY_CHECK.items():
            if check_name in self._skipped:
                continue
            total_groups_evaluated += 1

            best_missing: list[str] | None = None
            for group in groups:
                missing_in_group = [v for v in group if not os.environ.get(v)]
                if not missing_in_group:
                    best_missing = []
                    break
                if best_missing is None or len(missing_in_group) < len(best_missing):
                    best_missing = missing_in_group

            if best_missing:
                for v in best_missing:
                    if v not in seen:
                        seen.add(v)
                        union_missing.append(v)

        if union_missing:
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"Missing env vars: {', '.join(union_missing)}",
                details={"missing": union_missing},
            )
        return CheckResult(
            name=self.name,
            severity=Severity.OK,
            message=f"All {total_groups_evaluated} env requirement(s) satisfied",
        )


class DBConnectivityCheck:
    """Simple SELECT 1 to verify database is reachable."""

    name = "preflight.db_connectivity"

    def __init__(self, settings: RundaySettings) -> None:
        self._settings = settings

    async def run(self) -> CheckResult:
        t0 = time.monotonic()
        try:
            engine = get_engine()
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            latency_ms = int((time.monotonic() - t0) * 1000)
            return CheckResult(
                name=self.name,
                severity=Severity.OK,
                message=f"Database reachable ({latency_ms} ms)",
                details={"latency_ms": latency_ms},
                duration_ms=latency_ms,
            )
        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            logger.debug(f"DB check failed: {exc}")
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"Database unreachable: {exc}",
                details={"error": str(exc)},
                duration_ms=latency_ms,
            )


class AnthropicCheck:
    """Single-token API call to verify Anthropic connectivity and key validity."""

    name = "preflight.anthropic"

    def __init__(self, settings: RundaySettings) -> None:
        self._settings = settings

    async def run(self) -> CheckResult:
        if not self._settings.anthropic_api_key:
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message="ANTHROPIC_API_KEY not set",
            )
        t0 = time.monotonic()
        try:
            import anthropic

            client = anthropic.AsyncAnthropic(api_key=self._settings.anthropic_api_key)
            msg = await client.messages.create(
                model=self._settings.anthropic_model,
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
            _ = msg
            return CheckResult(
                name=self.name,
                severity=Severity.OK,
                message=f"Anthropic API reachable ({latency_ms} ms)",
                details={"latency_ms": latency_ms, "model": self._settings.anthropic_model},
                duration_ms=latency_ms,
            )
        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"Anthropic API error: {exc}",
                details={"error": str(exc)},
                duration_ms=latency_ms,
            )


class TelegramCheck:
    """Send a preflight ping to Telegram (suppressible with quiet=True)."""

    name = "preflight.telegram"

    def __init__(self, settings: RundaySettings, quiet: bool = False) -> None:
        self._settings = settings
        self._quiet = quiet

    async def run(self) -> CheckResult:
        if not self._settings.telegram_bot_token or not self._settings.telegram_chat_id:
            return CheckResult(
                name=self.name,
                severity=Severity.WARN,
                message="Telegram credentials not configured — skipping",
            )
        if self._quiet:
            return CheckResult(
                name=self.name,
                severity=Severity.OK,
                message="Telegram check suppressed (--quiet)",
            )
        t0 = time.monotonic()
        try:
            import pytz
            from datetime import datetime

            tz = pytz.timezone("Asia/Kolkata")
            now_ist = datetime.now(tz).strftime("%H:%M IST")
            text_msg = f"🟢 Laabh preflight at {now_ist}"
            url = f"https://api.telegram.org/bot{self._settings.telegram_bot_token}/sendMessage"
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(
                    url,
                    json={
                        "chat_id": self._settings.telegram_chat_id,
                        "text": text_msg,
                    },
                )
                r.raise_for_status()
            latency_ms = int((time.monotonic() - t0) * 1000)
            return CheckResult(
                name=self.name,
                severity=Severity.OK,
                message=f"Telegram message sent ({latency_ms} ms)",
                details={"latency_ms": latency_ms},
                duration_ms=latency_ms,
            )
        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"Telegram send failed: {exc}",
                details={"error": str(exc)},
                duration_ms=latency_ms,
            )


class AngelOneCheck:
    """Login + fetch underlying tick for the probe symbol."""

    name = "preflight.angel_one"

    def __init__(self, settings: RundaySettings) -> None:
        self._settings = settings

    async def run(self) -> CheckResult:
        s = self._settings
        if not s.angel_one_api_key or not s.angel_one_client_id:
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message="Angel One credentials not configured",
            )
        t0 = time.monotonic()
        try:
            import pyotp
            from SmartApi import SmartConnect  # type: ignore[import]

            totp = pyotp.TOTP(s.angel_one_totp_secret).now()
            smart = SmartConnect(api_key=s.angel_one_api_key)
            data = smart.generateSession(s.angel_one_client_id, s.angel_one_password, totp)
            if data.get("status") is False:
                raise RuntimeError(f"Login failed: {data.get('message')}")
            latency_ms = int((time.monotonic() - t0) * 1000)
            return CheckResult(
                name=self.name,
                severity=Severity.OK,
                message=f"Angel One login OK ({latency_ms} ms)",
                details={"latency_ms": latency_ms, "probe": s.runday_angel_probe_symbol},
                duration_ms=latency_ms,
            )
        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"Angel One error: {exc}",
                details={"error": str(exc)},
                duration_ms=latency_ms,
            )


class NSECheck:
    """Cookie warmup + option-chain index probe."""

    name = "preflight.nse"

    def __init__(self, settings: RundaySettings) -> None:
        self._settings = settings

    async def run(self) -> CheckResult:
        t0 = time.monotonic()
        symbol = self._settings.runday_nse_probe_symbol
        headers = {
            "User-Agent": self._settings.nse_user_agent,
            "Accept": "application/json",
            "Referer": "https://www.nseindia.com/",
        }
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                # Cookie warmup
                await client.get("https://www.nseindia.com/", headers=headers)
                # Option chain probe
                resp = await client.get(
                    f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}",
                    headers=headers,
                )
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()

            latency_ms = int((time.monotonic() - t0) * 1000)
            records = data.get("records", {})
            strike_count = len(records.get("data", []))
            timestamp = records.get("timestamp", "unknown")
            return CheckResult(
                name=self.name,
                severity=Severity.OK,
                message=f"NSE option chain OK — {strike_count} strikes, ts={timestamp} ({latency_ms} ms)",
                details={
                    "latency_ms": latency_ms,
                    "strike_count": strike_count,
                    "snapshot_timestamp": timestamp,
                    "symbol": symbol,
                },
                duration_ms=latency_ms,
            )
        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"NSE connectivity failed: {exc}",
                details={"error": str(exc)},
                duration_ms=latency_ms,
            )


class DhanCheck:
    """POST /v2/optionchain/expirylist for NIFTY — light auth-and-reach probe.

    Uses the dedicated expirylist endpoint rather than /v2/optionchain so the
    probe doesn't depend on a hardcoded expiry date being available; the
    response just enumerates valid expiries for the underlying.
    """

    name = "preflight.dhan"

    def __init__(self, settings: RundaySettings) -> None:
        self._settings = settings

    async def run(self) -> CheckResult:
        s = self._settings
        try:
            headers = await get_dhan_headers()
        except DhanAuthError as exc:
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=str(exc),
            )
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                # Field names match Dhan v2 spec used by src/fno/sources/dhan_source.py:
                # UnderlyingScrip (security id, 13 = NIFTY index) + UnderlyingSeg
                # (NOT "UnderlyingSegment"). Drift between this probe and the
                # production caller is what made the old version 400 in prod.
                resp = await client.post(
                    "https://api.dhan.co/v2/optionchain/expirylist",
                    headers=headers,
                    json={
                        "UnderlyingScrip": 13,
                        "UnderlyingSeg": "IDX_I",
                    },
                )
                resp.raise_for_status()
                body = resp.json()
            latency_ms = int((time.monotonic() - t0) * 1000)
            expiries = body.get("data", []) if isinstance(body, dict) else []
            return CheckResult(
                name=self.name,
                severity=Severity.OK,
                message=f"Dhan API reachable — {len(expiries)} expiries ({latency_ms} ms)",
                details={
                    "latency_ms": latency_ms,
                    "probe": s.runday_dhan_probe_symbol,
                    "expiry_count": len(expiries),
                },
                duration_ms=latency_ms,
            )
        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"Dhan API error: {exc}",
                details={"error": str(exc)},
                duration_ms=latency_ms,
            )


class GitHubCheck:
    """GET /repos/{repo} with PAT — reports rate-limit headroom."""

    name = "preflight.github"

    def __init__(self, settings: RundaySettings) -> None:
        self._settings = settings

    async def run(self) -> CheckResult:
        s = self._settings
        if not s.github_token:
            return CheckResult(
                name=self.name,
                severity=Severity.WARN,
                message="GITHUB_TOKEN not configured — issue_filer will skip",
            )
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"https://api.github.com/repos/{s.github_repo}",
                    headers={
                        "Authorization": f"Bearer {s.github_token}",
                        "Accept": "application/vnd.github+json",
                    },
                )
                resp.raise_for_status()
                remaining = int(resp.headers.get("x-ratelimit-remaining", -1))
                limit = int(resp.headers.get("x-ratelimit-limit", -1))
            latency_ms = int((time.monotonic() - t0) * 1000)
            severity = Severity.WARN if 0 <= remaining < 100 else Severity.OK
            return CheckResult(
                name=self.name,
                severity=severity,
                message=f"GitHub API OK — rate-limit {remaining}/{limit} remaining ({latency_ms} ms)",
                details={"latency_ms": latency_ms, "rate_remaining": remaining, "rate_limit": limit},
                duration_ms=latency_ms,
            )
        except Exception as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"GitHub API error: {exc}",
                details={"error": str(exc)},
                duration_ms=latency_ms,
            )
