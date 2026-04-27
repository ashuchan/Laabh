"""Structured JSON output for laabh-runday."""
from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Any

from src.runday.checks.base import CheckResult, Severity


class _DatetimeEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, datetime):
            return o.isoformat()
        return super().default(o)


def emit_results(results: list[CheckResult], extra: dict[str, Any] | None = None) -> str:
    """Serialize a list of CheckResult objects to a JSON string."""
    payload: dict[str, Any] = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "total": len(results),
        "summary": {
            "ok": sum(1 for r in results if r.severity == Severity.OK),
            "warn": sum(1 for r in results if r.severity == Severity.WARN),
            "fail": sum(1 for r in results if r.severity == Severity.FAIL),
        },
        "checks": [r.to_dict() for r in results],
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload, cls=_DatetimeEncoder, indent=2, ensure_ascii=False)


def print_results(results: list[CheckResult], extra: dict[str, Any] | None = None) -> None:
    """Print JSON results to stdout."""
    print(emit_results(results, extra))


def emit_status(data: dict[str, Any]) -> str:
    """Serialize the status dashboard data to JSON."""
    return json.dumps(
        {"generated_at": datetime.utcnow().isoformat() + "Z", **data},
        cls=_DatetimeEncoder,
        indent=2,
        ensure_ascii=False,
    )


def emit_report(data: dict[str, Any]) -> str:
    """Serialize the daily report data to JSON."""
    return json.dumps(
        {"generated_at": datetime.utcnow().isoformat() + "Z", **data},
        cls=_DatetimeEncoder,
        indent=2,
        ensure_ascii=False,
    )
