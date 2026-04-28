"""Base types for the runday check framework."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class Severity(str, Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass
class CheckResult:
    """Result of a single named check."""

    name: str
    severity: Severity
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    queried_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def passed(self) -> bool:
        return self.severity == Severity.OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "severity": self.severity.value,
            "message": self.message,
            "details": self.details,
            "duration_ms": self.duration_ms,
            "queried_at": self.queried_at.isoformat(),
        }


@runtime_checkable
class Check(Protocol):
    """Protocol every check class must satisfy."""

    name: str

    async def run(self) -> CheckResult:
        """Execute the check and return a result."""
        ...


def exit_code_for(results: list[CheckResult]) -> int:
    """Map a list of results to a CLI exit code.

    0  — all OK
    10 — at least one WARN, no FAIL
    20 — at least one FAIL
    """
    severities = {r.severity for r in results}
    if Severity.FAIL in severities:
        return 20
    if Severity.WARN in severities:
        return 10
    return 0
