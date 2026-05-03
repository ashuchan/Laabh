"""Signal lifecycle and risk-cap checks."""
from __future__ import annotations

import time
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import func, text

from src.db import session_scope
from src.runday.checks.base import CheckResult, Severity
from src.runday.config import RundaySettings


class TradingStatusCheck:
    """Snapshot of today's signal lifecycle counts."""

    name = "trading.status"

    def __init__(self, settings: RundaySettings, anchor_date: date | None = None) -> None:
        self._settings = settings
        self._anchor = anchor_date

    async def run(self) -> CheckResult:
        t0 = time.monotonic()
        today = self._anchor or date.today()
        try:
            async with session_scope() as session:
                result = await session.execute(
                    text(
                        "SELECT status, COUNT(*) as cnt "
                        "FROM fno_signals "
                        "WHERE DATE(proposed_at AT TIME ZONE 'UTC') = :today "
                        "GROUP BY status"
                    ),
                    {"today": today},
                )
                by_status: dict[str, int] = {r[0]: r[1] for r in result.fetchall()}

                pnl_result = await session.execute(
                    text(
                        "SELECT COALESCE(SUM(final_pnl), 0) FROM fno_signals "
                        "WHERE DATE(proposed_at AT TIME ZONE 'UTC') = :today "
                        "AND final_pnl IS NOT NULL"
                    ),
                    {"today": today},
                )
                day_pnl = float(pnl_result.scalar() or 0)

            latency_ms = int((time.monotonic() - t0) * 1000)
            proposed = by_status.get("proposed", 0)
            filled = by_status.get("paper_filled", 0) + by_status.get("active", 0)
            scaled_out = by_status.get("scaled_out_50", 0)
            closed_target = by_status.get("closed_target", 0)
            closed_stop = by_status.get("closed_stop", 0)
            closed_time = by_status.get("closed_time", 0)
            open_positions = filled + scaled_out
            max_open = self._settings.fno_phase4_max_open_positions

            details: dict[str, Any] = {
                "proposed": proposed,
                "filled": filled,
                "scaled_out": scaled_out,
                "closed_target": closed_target,
                "closed_stop": closed_stop,
                "closed_time": closed_time,
                "open_positions": open_positions,
                "max_open_positions": max_open,
                "day_pnl": day_pnl,
                "date": today.isoformat(),
                "by_status": by_status,
            }

            severity = Severity.OK
            if open_positions > max_open:
                severity = Severity.FAIL
                msg = (
                    f"RISK BREACH: {open_positions} open positions > max {max_open} | "
                    f"proposed={proposed} filled={filled} closed(T/S/Ti)={closed_target}/{closed_stop}/{closed_time} "
                    f"P&L=₹{day_pnl:,.0f}"
                )
            else:
                msg = (
                    f"proposed={proposed} filled={filled} scaled={scaled_out} "
                    f"closed(T/S/Ti)={closed_target}/{closed_stop}/{closed_time} "
                    f"open={open_positions}/{max_open} P&L=₹{day_pnl:,.0f} (paper)"
                )

            return CheckResult(
                name=self.name,
                severity=severity,
                message=msg,
                details=details,
                duration_ms=latency_ms,
            )
        except Exception as exc:
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"Trading status check error: {exc}",
                details={"error": str(exc)},
            )


class RiskCapCheck:
    """Assert open positions do not exceed the configured maximum."""

    name = "trading.risk_cap"

    def __init__(self, settings: RundaySettings) -> None:
        self._settings = settings

    async def run(self) -> CheckResult:
        t0 = time.monotonic()
        try:
            async with session_scope() as session:
                result = await session.execute(
                    text(
                        "SELECT COUNT(*) FROM fno_signals "
                        "WHERE status IN ('active','paper_filled','scaled_out_50')"
                    )
                )
                open_count = result.scalar() or 0

            latency_ms = int((time.monotonic() - t0) * 1000)
            max_open = self._settings.fno_phase4_max_open_positions

            if open_count > max_open:
                return CheckResult(
                    name=self.name,
                    severity=Severity.FAIL,
                    message=f"Risk cap breached: {open_count} open positions > max {max_open}",
                    details={"open_positions": open_count, "max_open": max_open},
                    duration_ms=latency_ms,
                )
            return CheckResult(
                name=self.name,
                severity=Severity.OK,
                message=f"Risk cap OK: {open_count}/{max_open} positions open",
                details={"open_positions": open_count, "max_open": max_open},
                duration_ms=latency_ms,
            )
        except Exception as exc:
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"Risk cap check error: {exc}",
                details={"error": str(exc)},
            )
