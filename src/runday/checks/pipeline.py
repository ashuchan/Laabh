"""Per-phase completeness checks for the F&O pipeline."""
from __future__ import annotations

import time
from datetime import date, datetime, timezone

import pytz
from sqlalchemy import func, select, text

from src.db import session_scope
from src.models.fno_candidate import FNOCandidate
from src.models.fno_collection_tier import FNOCollectionTier
from src.models.notification import Notification
from src.runday.checks.base import CheckResult, Severity
from src.runday.config import RundaySettings

_IST = pytz.timezone("Asia/Kolkata")


class TierRefreshCheck:
    """Assert fno_collection_tiers.updated_at >= today 06:00 IST."""

    name = "checkpoint.tier_refresh"

    def __init__(self, settings: RundaySettings, anchor_date: date | None = None) -> None:
        self._settings = settings
        self._anchor = anchor_date

    async def run(self) -> CheckResult:
        t0 = time.monotonic()
        today = self._anchor or date.today()
        threshold = _IST.localize(datetime(today.year, today.month, today.day, 6, 0, 0))
        threshold_utc = threshold.astimezone(timezone.utc)
        try:
            async with session_scope() as session:
                result = await session.execute(
                    text(
                        "SELECT MAX(updated_at) FROM fno_collection_tiers"
                    )
                )
                max_updated = result.scalar_one_or_none()

            latency_ms = int((time.monotonic() - t0) * 1000)
            if max_updated is None:
                return CheckResult(
                    name=self.name,
                    severity=Severity.FAIL,
                    message="fno_collection_tiers is empty — tier refresh has not run",
                    duration_ms=latency_ms,
                )

            # Ensure timezone-aware comparison
            if max_updated.tzinfo is None:
                max_updated = max_updated.replace(tzinfo=timezone.utc)

            if max_updated < threshold_utc:
                return CheckResult(
                    name=self.name,
                    severity=Severity.FAIL,
                    message=(
                        f"Tier refresh stale: last updated {max_updated.isoformat()}, "
                        f"required >= {threshold_utc.isoformat()}"
                    ),
                    details={"last_updated": max_updated.isoformat(), "threshold": threshold_utc.isoformat()},
                    duration_ms=latency_ms,
                )
            return CheckResult(
                name=self.name,
                severity=Severity.OK,
                message=f"Tier refresh current (updated {max_updated.isoformat()})",
                details={"last_updated": max_updated.isoformat()},
                duration_ms=latency_ms,
            )
        except Exception as exc:
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"Tier refresh check error: {exc}",
                details={"error": str(exc)},
            )


class Phase1Check:
    """Assert ≥30 rows in fno_candidates for phase=1 and run_date=today."""

    name = "checkpoint.phase1"

    def __init__(self, settings: RundaySettings, anchor_date: date | None = None) -> None:
        self._settings = settings
        self._anchor = anchor_date

    async def run(self) -> CheckResult:
        t0 = time.monotonic()
        today = self._anchor or date.today()
        try:
            async with session_scope() as session:
                result = await session.execute(
                    select(func.count()).where(
                        FNOCandidate.phase == 1,
                        FNOCandidate.run_date == today,
                    )
                )
                count = result.scalar() or 0

            latency_ms = int((time.monotonic() - t0) * 1000)
            min_required = self._settings.runday_min_phase1_candidates
            severity = Severity.OK if count >= min_required else Severity.FAIL

            return CheckResult(
                name=self.name,
                severity=severity,
                message=(
                    f"Phase 1: {count} candidates for {today.isoformat()} "
                    f"(required ≥{min_required})"
                ),
                details={"count": count, "min_required": min_required, "date": today.isoformat()},
                duration_ms=latency_ms,
            )
        except Exception as exc:
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"Phase 1 check error: {exc}",
                details={"error": str(exc)},
            )


class Phase2Check:
    """Assert exactly FNO_PHASE2_TARGET_OUTPUT rows with non-null composite_score."""

    name = "checkpoint.phase2"

    def __init__(self, settings: RundaySettings, anchor_date: date | None = None) -> None:
        self._settings = settings
        self._anchor = anchor_date

    async def run(self) -> CheckResult:
        t0 = time.monotonic()
        today = self._anchor or date.today()
        target = self._settings.fno_phase2_target_output
        try:
            async with session_scope() as session:
                total_result = await session.execute(
                    select(func.count()).where(
                        FNOCandidate.phase == 2,
                        FNOCandidate.run_date == today,
                    )
                )
                total = total_result.scalar() or 0

                scored_result = await session.execute(
                    select(func.count()).where(
                        FNOCandidate.phase == 2,
                        FNOCandidate.run_date == today,
                        FNOCandidate.composite_score != None,  # noqa: E711
                    )
                )
                scored = scored_result.scalar() or 0

            latency_ms = int((time.monotonic() - t0) * 1000)
            issues = []
            if total != target:
                issues.append(f"expected {target} rows, found {total}")
            if scored < total:
                issues.append(f"{total - scored} rows have null composite_score")

            severity = Severity.OK if not issues else Severity.FAIL
            return CheckResult(
                name=self.name,
                severity=severity,
                message=(
                    f"Phase 2: {total} candidates ({scored} scored) for {today.isoformat()}"
                    + (f" — {'; '.join(issues)}" if issues else "")
                ),
                details={
                    "total": total,
                    "scored": scored,
                    "target": target,
                    "date": today.isoformat(),
                },
                duration_ms=latency_ms,
            )
        except Exception as exc:
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"Phase 2 check error: {exc}",
                details={"error": str(exc)},
            )


class Phase3Check:
    """Assert FNO_PHASE3_TARGET_OUTPUT rows + matching llm_audit_log rows."""

    name = "checkpoint.phase3"

    def __init__(self, settings: RundaySettings, anchor_date: date | None = None) -> None:
        self._settings = settings
        self._anchor = anchor_date

    async def run(self) -> CheckResult:
        t0 = time.monotonic()
        today = self._anchor or date.today()
        target = self._settings.fno_phase3_target_output
        min_audit = self._settings.runday_expected_min_phase3_audit_rows
        try:
            async with session_scope() as session:
                candidate_result = await session.execute(
                    select(func.count()).where(
                        FNOCandidate.phase == 3,
                        FNOCandidate.run_date == today,
                    )
                )
                candidate_count = candidate_result.scalar() or 0

                audit_result = await session.execute(
                    text(
                        "SELECT COUNT(*) FROM llm_audit_log "
                        "WHERE caller = 'fno.thesis' "
                        "AND DATE(created_at AT TIME ZONE 'UTC') = :today"
                    ),
                    {"today": today},
                )
                audit_count = audit_result.scalar() or 0

            latency_ms = int((time.monotonic() - t0) * 1000)
            issues = []
            if candidate_count != target:
                issues.append(f"expected {target} phase-3 rows, found {candidate_count}")
            if audit_count < min_audit:
                issues.append(
                    f"llm_audit_log has {audit_count} rows for fno.thesis (expected ≥{min_audit})"
                )

            severity = Severity.OK if not issues else Severity.FAIL
            return CheckResult(
                name=self.name,
                severity=severity,
                message=(
                    f"Phase 3: {candidate_count} theses, {audit_count} LLM audit rows "
                    f"for {today.isoformat()}"
                    + (f" — {'; '.join(issues)}" if issues else "")
                ),
                details={
                    "candidate_count": candidate_count,
                    "audit_count": audit_count,
                    "target": target,
                    "date": today.isoformat(),
                },
                duration_ms=latency_ms,
            )
        except Exception as exc:
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"Phase 3 check error: {exc}",
                details={"error": str(exc)},
            )


class MorningBriefCheck:
    """Assert a morning-brief notification was sent today."""

    name = "checkpoint.morning_brief"

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
                        "SELECT title, pushed_at FROM notifications "
                        "WHERE (type = 'system' OR title ILIKE '%morning%brief%') "
                        "AND DATE(created_at AT TIME ZONE 'UTC') = :today "
                        "AND is_pushed = true "
                        "ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"today": today},
                )
                row = result.fetchone()

            latency_ms = int((time.monotonic() - t0) * 1000)
            if row is None:
                return CheckResult(
                    name=self.name,
                    severity=Severity.FAIL,
                    message=f"No morning brief notification sent for {today.isoformat()}",
                    details={"date": today.isoformat()},
                    duration_ms=latency_ms,
                )
            title, pushed_at = row
            return CheckResult(
                name=self.name,
                severity=Severity.OK,
                message=f"Morning brief sent: '{title}' at {pushed_at}",
                details={"title": title, "sent_at": str(pushed_at), "date": today.isoformat()},
                duration_ms=latency_ms,
            )
        except Exception as exc:
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"Morning brief check error: {exc}",
                details={"error": str(exc)},
            )


class Phase4EntryCheck:
    """Assert the entry loop has run ≥1 tick since 09:45 IST."""

    name = "checkpoint.phase4_entry"

    def __init__(self, settings: RundaySettings, anchor_date: date | None = None) -> None:
        self._settings = settings
        self._anchor = anchor_date

    async def run(self) -> CheckResult:
        t0 = time.monotonic()
        today = self._anchor or date.today()
        threshold_ist = _IST.localize(datetime(today.year, today.month, today.day, 9, 45, 0))
        threshold_utc = threshold_ist.astimezone(timezone.utc)
        try:
            async with session_scope() as session:
                result = await session.execute(
                    text(
                        "SELECT MAX(created_at) FROM job_log "
                        "WHERE job_name ILIKE '%phase4%entry%' "
                        "OR job_name ILIKE '%fno_entry%'"
                    )
                )
                last_run = result.scalar_one_or_none()

            latency_ms = int((time.monotonic() - t0) * 1000)
            if last_run is None:
                return CheckResult(
                    name=self.name,
                    severity=Severity.FAIL,
                    message="Phase 4 entry loop has not run today",
                    duration_ms=latency_ms,
                )
            if last_run.tzinfo is None:
                last_run = last_run.replace(tzinfo=timezone.utc)
            if last_run < threshold_utc:
                return CheckResult(
                    name=self.name,
                    severity=Severity.FAIL,
                    message=f"Phase 4 entry loop last ran at {last_run} — before 09:45 IST threshold",
                    details={"last_run": last_run.isoformat()},
                    duration_ms=latency_ms,
                )
            return CheckResult(
                name=self.name,
                severity=Severity.OK,
                message=f"Phase 4 entry loop last ran at {last_run.isoformat()}",
                details={"last_run": last_run.isoformat()},
                duration_ms=latency_ms,
            )
        except Exception as exc:
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"Phase 4 entry check error: {exc}",
                details={"error": str(exc)},
            )


class Phase4ManageCheck:
    """Assert the manage loop ran ≥1 tick in the last 5 min during 09:15–14:30.

    Pass `now` to simulate a specific wall-clock time (replay mode).
    When `now` is None, uses datetime.now(UTC) (live mode).
    """

    name = "checkpoint.phase4_manage"

    def __init__(
        self,
        settings: RundaySettings,
        anchor_date: date | None = None,
        *,
        now: datetime | None = None,
    ) -> None:
        self._settings = settings
        self._anchor = anchor_date
        self._now = now  # None = use real clock; set for replay simulated time

    async def run(self) -> CheckResult:
        t0 = time.monotonic()
        from datetime import timedelta
        now_utc = self._now if self._now is not None else datetime.now(timezone.utc)
        today = self._anchor or (self._now.date() if self._now else date.today())

        market_open = _IST.localize(datetime(today.year, today.month, today.day, 9, 15, 0))
        market_close = _IST.localize(datetime(today.year, today.month, today.day, 14, 30, 0))
        now_ist = now_utc.astimezone(_IST)

        if not (market_open <= now_ist <= market_close):
            return CheckResult(
                name=self.name,
                severity=Severity.WARN,
                message="Outside market hours (09:15–14:30 IST) — manage loop check skipped",
            )

        five_min_ago = now_utc - timedelta(minutes=5)

        try:
            async with session_scope() as session:
                result = await session.execute(
                    text(
                        "SELECT MAX(created_at) FROM job_log "
                        "WHERE (job_name ILIKE '%phase4%manage%' OR job_name ILIKE '%fno_manage%') "
                        "AND created_at >= :since"
                    ),
                    {"since": five_min_ago},
                )
                last_run = result.scalar_one_or_none()

            latency_ms = int((time.monotonic() - t0) * 1000)
            if last_run is None:
                return CheckResult(
                    name=self.name,
                    severity=Severity.FAIL,
                    message="Phase 4 manage loop has not run in the last 5 minutes",
                    duration_ms=latency_ms,
                )
            return CheckResult(
                name=self.name,
                severity=Severity.OK,
                message=f"Phase 4 manage loop active (last run: {last_run.isoformat()})",
                details={"last_run": last_run.isoformat()},
                duration_ms=latency_ms,
            )
        except Exception as exc:
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"Phase 4 manage check error: {exc}",
                details={"error": str(exc)},
            )


class HardExitCheck:
    """At 14:30+ assert all active positions transitioned to closed_*."""

    name = "checkpoint.hard_exit"

    def __init__(self, settings: RundaySettings, anchor_date: date | None = None) -> None:
        self._settings = settings
        self._anchor = anchor_date

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
            if open_count > 0:
                return CheckResult(
                    name=self.name,
                    severity=Severity.FAIL,
                    message=f"{open_count} position(s) still active after hard-exit time",
                    details={"open_positions": open_count},
                    duration_ms=latency_ms,
                )
            return CheckResult(
                name=self.name,
                severity=Severity.OK,
                message="All positions closed — hard exit complete",
                details={"open_positions": 0},
                duration_ms=latency_ms,
            )
        except Exception as exc:
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"Hard exit check error: {exc}",
                details={"error": str(exc)},
            )


class ReviewLoopCheck:
    """Assert review-loop ran today and all issues have github_issue_url."""

    name = "checkpoint.review_loop"

    def __init__(self, settings: RundaySettings, anchor_date: date | None = None) -> None:
        self._settings = settings
        self._anchor = anchor_date

    async def run(self) -> CheckResult:
        t0 = time.monotonic()
        today = self._anchor or date.today()
        try:
            async with session_scope() as session:
                loop_result = await session.execute(
                    text(
                        "SELECT COUNT(*) FROM job_log "
                        "WHERE job_name ILIKE '%review%loop%' "
                        "AND DATE(created_at AT TIME ZONE 'UTC') = :today"
                    ),
                    {"today": today},
                )
                loop_runs = loop_result.scalar() or 0

                unfiled_result = await session.execute(
                    text(
                        "SELECT COUNT(*) FROM chain_collection_issues "
                        "WHERE DATE(detected_at AT TIME ZONE 'UTC') = :today "
                        "AND github_issue_url IS NULL "
                        "AND resolved_at IS NULL"
                    ),
                    {"today": today},
                )
                unfiled = unfiled_result.scalar() or 0

            latency_ms = int((time.monotonic() - t0) * 1000)
            issues = []
            if loop_runs == 0:
                issues.append("review-loop did not run today")
            if unfiled > 0:
                issues.append(f"{unfiled} chain issue(s) not yet filed to GitHub")

            severity = Severity.OK if not issues else Severity.FAIL
            return CheckResult(
                name=self.name,
                severity=severity,
                message=(
                    f"Review loop: {loop_runs} run(s) today, {unfiled} unfiled issues"
                    + (f" — {'; '.join(issues)}" if issues else "")
                ),
                details={
                    "loop_runs": loop_runs,
                    "unfiled_issues": unfiled,
                    "date": today.isoformat(),
                },
                duration_ms=latency_ms,
            )
        except Exception as exc:
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"Review loop check error: {exc}",
                details={"error": str(exc)},
            )


# Registry mapping phase names → check factory functions
def make_phase_check(
    phase: str,
    settings: RundaySettings,
    anchor_date: date | None = None,
    *,
    now: "datetime | None" = None,
) -> "CheckResult | None":
    """Return the appropriate check instance for the given phase name, or None.

    Pass ``now`` to override the wall-clock for checks that support simulated
    time (currently ``Phase4ManageCheck``).
    """
    _MAP = {
        "tier-refresh": TierRefreshCheck,
        "phase1": Phase1Check,
        "phase2": Phase2Check,
        "phase3": Phase3Check,
        "morning-brief": MorningBriefCheck,
        "phase4-entry": Phase4EntryCheck,
        "phase4-manage": Phase4ManageCheck,
        "hard-exit": HardExitCheck,
        "review-loop": ReviewLoopCheck,
    }
    cls = _MAP.get(phase)
    if cls is None:
        return None
    if cls is Phase4ManageCheck and now is not None:
        return cls(settings, anchor_date, now=now)
    return cls(settings, anchor_date)
