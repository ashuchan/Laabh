"""Data-quality checks: tier table, trading day, VIX, IV history, ban list."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta

import pytz
from sqlalchemy import func, select, text

from src.db import session_scope
from src.models.fno_ban import FNOBanList
from src.models.fno_collection_tier import FNOCollectionTier
from src.models.fno_iv import IVHistory
from src.models.fno_vix import VIXTick
from src.models.instrument import Instrument
from src.runday.checks.base import CheckResult, Severity
from src.runday.config import RundaySettings

_IST = pytz.timezone("Asia/Kolkata")


class TierTableCheck:
    """Assert fno_collection_tiers has expected row counts for Tier 1 and Tier 2."""

    name = "preflight.tier_table"

    def __init__(self, settings: RundaySettings) -> None:
        self._settings = settings

    async def run(self) -> CheckResult:
        t0 = time.monotonic()
        try:
            async with session_scope() as session:
                result = await session.execute(
                    text(
                        "SELECT tier, COUNT(*) AS cnt FROM fno_collection_tiers GROUP BY tier"
                    )
                )
                rows = {r[0]: r[1] for r in result.fetchall()}

            latency_ms = int((time.monotonic() - t0) * 1000)
            tier1_count = rows.get(1, 0)
            tier2_count = rows.get(2, 0)
            total = tier1_count + tier2_count

            if total == 0:
                return CheckResult(
                    name=self.name,
                    severity=Severity.FAIL,
                    message="fno_collection_tiers is empty — run tier refresh",
                    details={"tier1": 0, "tier2": 0},
                    duration_ms=latency_ms,
                )

            expected_tier1 = self._settings.fno_tier1_size
            severity = Severity.OK
            msg_parts = [f"Tier 1: {tier1_count} (expected {expected_tier1}), Tier 2: {tier2_count}"]
            if tier1_count != expected_tier1:
                severity = Severity.WARN
                msg_parts.append(f"Tier 1 count differs from FNO_TIER1_SIZE={expected_tier1}")

            return CheckResult(
                name=self.name,
                severity=severity,
                message="; ".join(msg_parts),
                details={"tier1": tier1_count, "tier2": tier2_count, "total": total},
                duration_ms=latency_ms,
            )
        except Exception as exc:
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"Tier table check error: {exc}",
                details={"error": str(exc)},
            )


class TradingDayCheck:
    """Verify tomorrow is a trading day (not weekend or holiday)."""

    name = "preflight.trading_day"

    def __init__(self, settings: RundaySettings, anchor_date: date | None = None) -> None:
        self._settings = settings
        self._anchor = anchor_date

    async def run(self) -> CheckResult:
        t0 = time.monotonic()
        target = (self._anchor or date.today()) + timedelta(days=1)

        # Weekend check
        if target.weekday() >= 5:
            day_name = "Saturday" if target.weekday() == 5 else "Sunday"
            return CheckResult(
                name=self.name,
                severity=Severity.WARN,
                message=f"{target.isoformat()} is a {day_name} — no trading",
                details={"date": target.isoformat(), "reason": day_name},
            )

        try:
            async with session_scope() as session:
                holiday_result = await session.execute(
                    text("SELECT value FROM system_config WHERE key = 'holiday_calendar'")
                )
                row = holiday_result.scalar_one_or_none()

            latency_ms = int((time.monotonic() - t0) * 1000)
            holidays: list[str] = []
            if row and isinstance(row, dict):
                holidays = row.get("dates", [])
            elif row and isinstance(row, list):
                holidays = row

            target_str = target.isoformat()
            if target_str in holidays:
                return CheckResult(
                    name=self.name,
                    severity=Severity.WARN,
                    message=f"{target_str} is a market holiday",
                    details={"date": target_str, "reason": "holiday"},
                    duration_ms=latency_ms,
                )
            return CheckResult(
                name=self.name,
                severity=Severity.OK,
                message=f"{target_str} is a trading day",
                details={"date": target_str},
                duration_ms=latency_ms,
            )
        except Exception as exc:
            return CheckResult(
                name=self.name,
                severity=Severity.WARN,
                message=f"Could not verify holiday calendar: {exc}",
                details={"error": str(exc)},
            )


class IVHistoryCoverageCheck:
    """Assert IV history rows exist for today for ≥90% of F&O instruments."""

    name = "checkpoint.iv_history"

    def __init__(self, settings: RundaySettings, anchor_date: date | None = None) -> None:
        self._settings = settings
        self._anchor = anchor_date

    async def run(self) -> CheckResult:
        t0 = time.monotonic()
        today = self._anchor or date.today()
        try:
            async with session_scope() as session:
                total_result = await session.execute(
                    select(func.count()).where(
                        Instrument.is_fno == True,  # noqa: E712
                        Instrument.is_active == True,  # noqa: E712
                    )
                )
                total_fno = total_result.scalar() or 0

                iv_result = await session.execute(
                    select(func.count()).where(IVHistory.date == today)
                )
                iv_count = iv_result.scalar() or 0

            latency_ms = int((time.monotonic() - t0) * 1000)
            if total_fno == 0:
                return CheckResult(
                    name=self.name,
                    severity=Severity.WARN,
                    message="No F&O instruments found in instruments table",
                    duration_ms=latency_ms,
                )

            coverage_pct = (iv_count / total_fno) * 100
            min_pct = self._settings.runday_min_iv_history_coverage_pct
            severity = Severity.OK if coverage_pct >= min_pct else Severity.FAIL

            return CheckResult(
                name=self.name,
                severity=severity,
                message=(
                    f"IV history coverage: {iv_count}/{total_fno} instruments "
                    f"({coverage_pct:.1f}%) for {today.isoformat()}"
                ),
                details={
                    "date": today.isoformat(),
                    "iv_rows": iv_count,
                    "total_fno": total_fno,
                    "coverage_pct": round(coverage_pct, 2),
                    "min_required_pct": min_pct,
                },
                duration_ms=latency_ms,
            )
        except Exception as exc:
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"IV history check error: {exc}",
                details={"error": str(exc)},
            )


class BanListCheck:
    """Assert a ban-list entry exists for today (or confirm it was explicitly empty)."""

    name = "checkpoint.ban_list"

    def __init__(self, settings: RundaySettings, anchor_date: date | None = None) -> None:
        self._settings = settings
        self._anchor = anchor_date

    async def run(self) -> CheckResult:
        t0 = time.monotonic()
        today = self._anchor or date.today()
        try:
            async with session_scope() as session:
                result = await session.execute(
                    select(func.count()).where(FNOBanList.ban_date == today)
                )
                count = result.scalar() or 0

                # Check if a "clean day" marker exists in system_config
                clean_result = await session.execute(
                    text(
                        "SELECT value FROM system_config "
                        "WHERE key = :key",
                    ),
                    {"key": f"ban_list_empty_{today.isoformat()}"},
                )
                clean_marker = clean_result.scalar_one_or_none()

            latency_ms = int((time.monotonic() - t0) * 1000)

            if count > 0:
                return CheckResult(
                    name=self.name,
                    severity=Severity.OK,
                    message=f"Ban list fetched: {count} banned instrument(s) for {today.isoformat()}",
                    details={"date": today.isoformat(), "banned_count": count},
                    duration_ms=latency_ms,
                )
            if clean_marker is not None:
                return CheckResult(
                    name=self.name,
                    severity=Severity.OK,
                    message=f"Ban list fetched: 0 bans for {today.isoformat()} (clean day marker present)",
                    details={"date": today.isoformat(), "banned_count": 0},
                    duration_ms=latency_ms,
                )
            return CheckResult(
                name=self.name,
                severity=Severity.WARN,
                message=f"No ban-list rows for {today.isoformat()} — fetch may not have run yet",
                details={"date": today.isoformat(), "banned_count": 0},
                duration_ms=latency_ms,
            )
        except Exception as exc:
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"Ban list check error: {exc}",
                details={"error": str(exc)},
            )
