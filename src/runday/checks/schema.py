"""Schema checks: migrations applied, tables exist, seed data present."""
from __future__ import annotations

import subprocess
import time
from typing import Sequence

from sqlalchemy import text

from src.db import get_engine
from src.runday.checks.base import CheckResult, Severity
from src.runday.config import RundaySettings

_REQUIRED_TABLES = [
    "instruments",
    "fno_candidates",
    "fno_signals",
    "fno_signal_events",
    "fno_collection_tiers",
    "chain_collection_log",
    "chain_collection_issues",
    "source_health",
    "iv_history",
    "fno_ban_list",
    "fno_ban_list",
    "vix_ticks",
    "llm_audit_log",
    "notifications",
    "job_log",
    "system_config",
    "data_sources",
]


class MigrationsCurrentCheck:
    """Verify alembic current head matches alembic heads."""

    name = "preflight.migrations_current"

    def __init__(self, settings: RundaySettings) -> None:
        self._settings = settings

    async def run(self) -> CheckResult:
        t0 = time.monotonic()
        try:
            heads_proc = subprocess.run(
                ["alembic", "heads"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            current_proc = subprocess.run(
                ["alembic", "current"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            latency_ms = int((time.monotonic() - t0) * 1000)

            if heads_proc.returncode != 0 or current_proc.returncode != 0:
                return CheckResult(
                    name=self.name,
                    severity=Severity.FAIL,
                    message="alembic command failed — check alembic.ini",
                    details={
                        "heads_stderr": heads_proc.stderr,
                        "current_stderr": current_proc.stderr,
                    },
                    duration_ms=latency_ms,
                )

            # Parse revision IDs from alembic output
            heads = _parse_revisions(heads_proc.stdout)
            current = _parse_revisions(current_proc.stdout)

            unapplied = heads - current
            if unapplied:
                return CheckResult(
                    name=self.name,
                    severity=Severity.FAIL,
                    message=f"Unapplied migrations: {', '.join(sorted(unapplied))}",
                    details={"heads": sorted(heads), "current": sorted(current)},
                    duration_ms=latency_ms,
                )
            return CheckResult(
                name=self.name,
                severity=Severity.OK,
                message=f"Migrations current ({', '.join(sorted(current))})",
                details={"current": sorted(current)},
                duration_ms=latency_ms,
            )
        except FileNotFoundError:
            return CheckResult(
                name=self.name,
                severity=Severity.WARN,
                message="alembic not found in PATH — skipping migration check",
            )
        except Exception as exc:
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"Migration check error: {exc}",
                details={"error": str(exc)},
            )


def _parse_revisions(output: str) -> set[str]:
    """Extract revision hashes from alembic output lines."""
    revisions: set[str] = set()
    for line in output.splitlines():
        parts = line.strip().split()
        if parts:
            rev = parts[0].rstrip("(")
            if len(rev) >= 8 and all(c in "0123456789abcdef_" for c in rev.lower()):
                revisions.add(rev)
    return revisions


class RequiredTablesCheck:
    """Assert all F&O tables exist in the database."""

    name = "preflight.required_tables"

    def __init__(self, settings: RundaySettings, tables: Sequence[str] | None = None) -> None:
        self._settings = settings
        self._tables = list(tables or _REQUIRED_TABLES)

    async def run(self) -> CheckResult:
        t0 = time.monotonic()
        try:
            engine = get_engine()
            async with engine.connect() as conn:
                result = await conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public'"
                    )
                )
                existing = {row[0] for row in result.fetchall()}

            latency_ms = int((time.monotonic() - t0) * 1000)
            missing = sorted(set(self._tables) - existing)
            if missing:
                return CheckResult(
                    name=self.name,
                    severity=Severity.FAIL,
                    message=f"Missing tables: {', '.join(missing)}",
                    details={"missing": missing, "existing_count": len(existing)},
                    duration_ms=latency_ms,
                )
            return CheckResult(
                name=self.name,
                severity=Severity.OK,
                message=f"All {len(self._tables)} required tables present",
                details={"checked": self._tables},
                duration_ms=latency_ms,
            )
        except Exception as exc:
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"Table check error: {exc}",
                details={"error": str(exc)},
            )


class SeedDataCheck:
    """Verify source_health rows and system_config holiday calendar exist."""

    name = "preflight.seed_data"

    _REQUIRED_SOURCES = ("nse", "dhan", "angel_one")

    def __init__(self, settings: RundaySettings) -> None:
        self._settings = settings

    async def run(self) -> CheckResult:
        t0 = time.monotonic()
        try:
            engine = get_engine()
            async with engine.connect() as conn:
                src_result = await conn.execute(
                    text("SELECT source FROM source_health WHERE source = ANY(:sources)"),
                    {"sources": list(self._REQUIRED_SOURCES)},
                )
                found_sources = {row[0] for row in src_result.fetchall()}

                holiday_result = await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM system_config "
                        "WHERE key = 'holiday_calendar'"
                    )
                )
                holiday_count = holiday_result.scalar() or 0

            latency_ms = int((time.monotonic() - t0) * 1000)
            missing_sources = sorted(set(self._REQUIRED_SOURCES) - found_sources)
            issues = []
            if missing_sources:
                issues.append(f"source_health missing: {', '.join(missing_sources)}")
            if not holiday_count:
                issues.append("system_config has no holiday_calendar entry")

            if issues:
                return CheckResult(
                    name=self.name,
                    severity=Severity.WARN,
                    message="; ".join(issues),
                    details={"missing_sources": missing_sources, "holiday_calendar": bool(holiday_count)},
                    duration_ms=latency_ms,
                )
            return CheckResult(
                name=self.name,
                severity=Severity.OK,
                message="Seed data present (source_health + holiday calendar)",
                details={"sources": sorted(found_sources), "holiday_calendar": True},
                duration_ms=latency_ms,
            )
        except Exception as exc:
            return CheckResult(
                name=self.name,
                severity=Severity.FAIL,
                message=f"Seed data check error: {exc}",
                details={"error": str(exc)},
            )
