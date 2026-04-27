"""LLM audit log presence and latency checks."""
from __future__ import annotations

import time
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import text

from src.db import session_scope
from src.runday.checks.base import CheckResult, Severity
from src.runday.config import RundaySettings


class LLMAuditCheck:
    """Verify LLM audit log has expected rows for today and report latency stats."""

    name = "audit.llm_log"

    def __init__(
        self,
        settings: RundaySettings,
        caller: str = "fno.thesis",
        anchor_date: date | None = None,
    ) -> None:
        self._settings = settings
        self._caller = caller
        self._anchor = anchor_date

    async def run(self) -> CheckResult:
        t0 = time.monotonic()
        today = self._anchor or date.today()
        try:
            async with session_scope() as session:
                result = await session.execute(
                    text(
                        """
                        SELECT
                            COUNT(*) as row_count,
                            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY latency_ms) as p50,
                            PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms) as p95,
                            PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY latency_ms) as p99,
                            SUM(tokens_in) as total_tokens_in,
                            SUM(tokens_out) as total_tokens_out
                        FROM llm_audit_log
                        WHERE caller = :caller
                        AND DATE(created_at AT TIME ZONE 'UTC') = :today
                        """
                    ),
                    {"caller": self._caller, "today": today.isoformat()},
                )
                row = result.fetchone()

            latency_ms = int((time.monotonic() - t0) * 1000)
            if row is None:
                return CheckResult(
                    name=self.name,
                    severity=Severity.WARN,
                    message=f"No LLM audit rows for caller='{self._caller}' today",
                    duration_ms=latency_ms,
                )

            count, p50, p95, p99, tokens_in, tokens_out = row
            min_rows = self._settings.runday_expected_min_phase3_audit_rows

            details: dict[str, Any] = {
                "caller": self._caller,
                "date": today.isoformat(),
                "row_count": count or 0,
                "latency_p50_ms": round(p50) if p50 else None,
                "latency_p95_ms": round(p95) if p95 else None,
                "latency_p99_ms": round(p99) if p99 else None,
                "total_tokens_in": tokens_in or 0,
                "total_tokens_out": tokens_out or 0,
            }

            severity = Severity.OK if (count or 0) >= min_rows else Severity.FAIL
            return CheckResult(
                name=self.name,
                severity=severity,
                message=(
                    f"LLM audit [{self._caller}]: {count or 0} rows "
                    f"(p50={round(p50) if p50 else 'n/a'}ms "
                    f"p95={round(p95) if p95 else 'n/a'}ms) "
                    f"tokens_in={tokens_in or 0} tokens_out={tokens_out or 0}"
                ),
                details=details,
                duration_ms=latency_ms,
            )
        except Exception as exc:
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"LLM audit check error: {exc}",
                details={"error": str(exc)},
            )


class LLMAuditSummaryCheck:
    """Summarize all LLM callers for the daily report."""

    name = "audit.llm_summary"

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
                        """
                        SELECT
                            caller,
                            COUNT(*) as row_count,
                            AVG(latency_ms) as avg_latency_ms,
                            SUM(tokens_in) as total_tokens_in,
                            SUM(tokens_out) as total_tokens_out
                        FROM llm_audit_log
                        WHERE DATE(created_at AT TIME ZONE 'UTC') = :today
                        GROUP BY caller
                        ORDER BY row_count DESC
                        """
                    ),
                    {"today": today.isoformat()},
                )
                rows = result.fetchall()

            latency_ms = int((time.monotonic() - t0) * 1000)
            if not rows:
                return CheckResult(
                    name=self.name,
                    severity=Severity.OK,
                    message=f"No LLM calls recorded for {today.isoformat()}",
                    details={"date": today.isoformat(), "callers": []},
                    duration_ms=latency_ms,
                )

            callers = []
            total_rows = 0
            total_tokens_in = 0
            total_tokens_out = 0
            for caller, count, avg_lat, t_in, t_out in rows:
                callers.append(
                    {
                        "caller": caller,
                        "row_count": count,
                        "avg_latency_ms": round(avg_lat) if avg_lat else None,
                        "tokens_in": t_in or 0,
                        "tokens_out": t_out or 0,
                    }
                )
                total_rows += count
                total_tokens_in += t_in or 0
                total_tokens_out += t_out or 0

            # Rough cost estimate: ~$3/1M tokens for claude-sonnet
            cost_usd = (total_tokens_in * 3.0 + total_tokens_out * 15.0) / 1_000_000

            return CheckResult(
                name=self.name,
                severity=Severity.OK,
                message=(
                    f"LLM activity: {total_rows} calls across {len(callers)} callers | "
                    f"tokens in={total_tokens_in:,} out={total_tokens_out:,} | "
                    f"est. cost ~${cost_usd:.4f}"
                ),
                details={
                    "date": today.isoformat(),
                    "callers": callers,
                    "total_rows": total_rows,
                    "total_tokens_in": total_tokens_in,
                    "total_tokens_out": total_tokens_out,
                    "estimated_cost_usd": round(cost_usd, 4),
                },
                duration_ms=latency_ms,
            )
        except Exception as exc:
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"LLM audit summary error: {exc}",
                details={"error": str(exc)},
            )
