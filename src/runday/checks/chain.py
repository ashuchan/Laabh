"""Chain collection health checks."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text

from src.db import session_scope
from src.models.fno_chain_issue import ChainCollectionIssue
from src.models.fno_chain_log import ChainCollectionLog
from src.models.fno_collection_tier import FNOCollectionTier
from src.models.fno_source_health import SourceHealth
from src.models.instrument import Instrument
from src.runday.checks.base import CheckResult, Severity
from src.runday.config import RundaySettings


class ChainCollectionHealthCheck:
    """Aggregate chain collection stats for the last N minutes."""

    name = "chain.collection_health"

    def __init__(self, settings: RundaySettings, lookback_minutes: int = 10) -> None:
        self._settings = settings
        self._lookback_minutes = lookback_minutes

    async def run(self) -> CheckResult:
        t0 = time.monotonic()
        since = datetime.now(timezone.utc) - timedelta(minutes=self._lookback_minutes)
        try:
            async with session_scope() as session:
                result = await session.execute(
                    text(
                        "SELECT status, COUNT(*) as cnt, "
                        "PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_ms) as p95_latency "
                        "FROM chain_collection_log "
                        "WHERE attempted_at >= :since "
                        "GROUP BY status"
                    ),
                    {"since": since},
                )
                rows = result.fetchall()

                # NSE share of successful collections
                nse_result = await session.execute(
                    text(
                        "SELECT COUNT(*) FROM chain_collection_log "
                        "WHERE attempted_at >= :since AND final_source = 'nse'"
                    ),
                    {"since": since},
                )
                nse_count = nse_result.scalar() or 0

                # Tier breakdown for latency
                tier1_latency_result = await session.execute(
                    text(
                        "SELECT PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY ccl.latency_ms) "
                        "FROM chain_collection_log ccl "
                        "JOIN fno_collection_tiers fct ON ccl.instrument_id = fct.instrument_id "
                        "WHERE ccl.attempted_at >= :since AND fct.tier = 1"
                    ),
                    {"since": since},
                )
                tier1_p95 = tier1_latency_result.scalar()

                tier2_latency_result = await session.execute(
                    text(
                        "SELECT PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY ccl.latency_ms) "
                        "FROM chain_collection_log ccl "
                        "JOIN fno_collection_tiers fct ON ccl.instrument_id = fct.instrument_id "
                        "WHERE ccl.attempted_at >= :since AND fct.tier = 2"
                    ),
                    {"since": since},
                )
                tier2_p95 = tier2_latency_result.scalar()

            latency_ms = int((time.monotonic() - t0) * 1000)

            stats: dict[str, int] = {"ok": 0, "fallback_used": 0, "missed": 0}
            for row in rows:
                stats[row[0]] = row[1]

            total = sum(stats.values())
            if total == 0:
                return CheckResult(
                    name=self.name,
                    severity=Severity.WARN,
                    message=f"No chain collection log entries in last {self._lookback_minutes} min",
                    details={"lookback_minutes": self._lookback_minutes},
                    duration_ms=latency_ms,
                )

            ok_pct = (stats["ok"] / total) * 100
            fallback_pct = (stats["fallback_used"] / total) * 100
            missed_pct = (stats["missed"] / total) * 100
            nse_share_pct = (nse_count / total) * 100 if total > 0 else 0.0

            issues = []
            severity = Severity.OK

            if missed_pct > self._settings.runday_max_acceptable_missed_pct:
                severity = Severity.FAIL
                issues.append(f"missed rate {missed_pct:.1f}% exceeds threshold")
            if nse_share_pct < self._settings.runday_min_chain_nse_share_pct:
                severity = Severity.WARN if severity == Severity.OK else severity
                issues.append(f"NSE share {nse_share_pct:.1f}% below {self._settings.runday_min_chain_nse_share_pct}%")
            if tier1_p95 and tier1_p95 > self._settings.runday_max_tier1_latency_ms_p95:
                severity = Severity.WARN if severity == Severity.OK else severity
                issues.append(f"Tier 1 p95 latency {tier1_p95:.0f}ms exceeds cap")
            if tier2_p95 and tier2_p95 > self._settings.runday_max_tier2_latency_ms_p95:
                severity = Severity.WARN if severity == Severity.OK else severity
                issues.append(f"Tier 2 p95 latency {tier2_p95:.0f}ms exceeds cap")

            return CheckResult(
                name=self.name,
                severity=severity,
                message=(
                    f"Chain: {total} attempts | ok={stats['ok']} ({ok_pct:.0f}%) "
                    f"fallback={stats['fallback_used']} ({fallback_pct:.0f}%) "
                    f"missed={stats['missed']} ({missed_pct:.0f}%) | "
                    f"NSE share={nse_share_pct:.1f}%"
                    + (f" | ISSUES: {'; '.join(issues)}" if issues else "")
                ),
                details={
                    "total": total,
                    "ok": stats["ok"],
                    "fallback": stats["fallback_used"],
                    "missed": stats["missed"],
                    "ok_pct": round(ok_pct, 2),
                    "fallback_pct": round(fallback_pct, 2),
                    "missed_pct": round(missed_pct, 2),
                    "nse_share_pct": round(nse_share_pct, 2),
                    "tier1_p95_latency_ms": round(tier1_p95) if tier1_p95 else None,
                    "tier2_p95_latency_ms": round(tier2_p95) if tier2_p95 else None,
                    "lookback_minutes": self._lookback_minutes,
                },
                duration_ms=latency_ms,
            )
        except Exception as exc:
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"Chain health check error: {exc}",
                details={"error": str(exc)},
            )


class SourceHealthCheck:
    """Check health status of all chain data sources."""

    name = "chain.source_health"

    def __init__(self, settings: RundaySettings) -> None:
        self._settings = settings

    async def run(self) -> CheckResult:
        t0 = time.monotonic()
        try:
            async with session_scope() as session:
                result = await session.execute(select(SourceHealth))
                rows = list(result.scalars())

            latency_ms = int((time.monotonic() - t0) * 1000)
            if not rows:
                return CheckResult(
                    name=self.name,
                    severity=Severity.WARN,
                    message="No source_health rows found",
                    duration_ms=latency_ms,
                )

            degraded = [r.source for r in rows if r.status in ("degraded", "failed")]
            details: dict[str, Any] = {
                r.source: {
                    "status": r.status,
                    "consecutive_errors": r.consecutive_errors,
                    "last_error_at": str(r.last_error_at) if r.last_error_at else None,
                    "last_error": r.last_error,
                }
                for r in rows
            }

            if degraded:
                return CheckResult(
                    name=self.name,
                    severity=Severity.FAIL,
                    message=f"Degraded sources: {', '.join(degraded)}",
                    details=details,
                    duration_ms=latency_ms,
                )
            return CheckResult(
                name=self.name,
                severity=Severity.OK,
                message=f"All {len(rows)} sources healthy",
                details=details,
                duration_ms=latency_ms,
            )
        except Exception as exc:
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"Source health check error: {exc}",
                details={"error": str(exc)},
            )


class OpenIssuesCheck:
    """Count unresolved chain collection issues."""

    name = "chain.open_issues"

    def __init__(self, settings: RundaySettings) -> None:
        self._settings = settings

    async def run(self) -> CheckResult:
        t0 = time.monotonic()
        try:
            async with session_scope() as session:
                result = await session.execute(
                    text(
                        "SELECT issue_type, COUNT(*) FROM chain_collection_issues "
                        "WHERE resolved_at IS NULL "
                        "GROUP BY issue_type"
                    )
                )
                rows = result.fetchall()

            latency_ms = int((time.monotonic() - t0) * 1000)
            by_type: dict[str, int] = {}
            for row in rows:
                by_type[row[0]] = row[1]
            total = sum(by_type.values())

            severity = Severity.FAIL if total > 0 else Severity.OK
            return CheckResult(
                name=self.name,
                severity=severity,
                message=f"Open issues: {total} total — {by_type}",
                details={"by_type": by_type, "total": total},
                duration_ms=latency_ms,
            )
        except Exception as exc:
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"Open issues check error: {exc}",
                details={"error": str(exc)},
            )


async def get_tier_breakdown(
    settings: RundaySettings,
    lookback_minutes: int = 60,
    tier_filter: int | None = None,
    only_degraded: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Query per-instrument chain collection stats for tier-check subcommand."""
    since = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    query = text(
        """
        SELECT
            i.symbol,
            fct.tier,
            MAX(ccl.attempted_at) AS last_attempt,
            MODE() WITHIN GROUP (ORDER BY ccl.status) AS last_status,
            ROUND(
                100.0 * SUM(CASE WHEN ccl.status = 'ok' THEN 1 ELSE 0 END)
                / NULLIF(COUNT(*), 0),
                1
            ) AS success_rate_1h,
            json_object_agg(
                COALESCE(ccl.final_source, 'unknown'),
                COUNT(*)
            ) AS source_breakdown
        FROM chain_collection_log ccl
        JOIN instruments i ON ccl.instrument_id = i.id
        JOIN fno_collection_tiers fct ON ccl.instrument_id = fct.instrument_id
        WHERE ccl.attempted_at >= :since
        GROUP BY i.symbol, fct.tier
        ORDER BY success_rate_1h ASC NULLS LAST, i.symbol
        LIMIT :limit
        """
    )
    async with session_scope() as session:
        result = await session.execute(query, {"since": since, "limit": limit})
        rows = result.fetchall()

    output = []
    for row in rows:
        symbol, tier, last_attempt, last_status, success_rate, source_breakdown = row
        if tier_filter is not None and tier != tier_filter:
            continue
        if only_degraded and (success_rate is None or success_rate >= 80.0):
            continue
        output.append(
            {
                "symbol": symbol,
                "tier": tier,
                "last_attempt": str(last_attempt) if last_attempt else None,
                "last_status": last_status,
                "success_rate_1h": float(success_rate) if success_rate is not None else None,
                "source_breakdown": source_breakdown or {},
            }
        )
    return output
