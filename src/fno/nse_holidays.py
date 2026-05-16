"""NSE trading-holiday loader for backfill scripts.

Loads a JSON list of NSE/BSE market holidays so the prereqs and LLM-feature
backfill scripts skip dates the exchange was closed. Source priority:

  1. Path in env var ``LAABH_NSE_HOLIDAYS_FILE``.
  2. ``<project>/database/nse_holidays.json``.
  3. Empty set, with a one-shot WARNING logged. This keeps the scripts
     functional but means weekends are the only filter — the operator
     should populate the file.

JSON format:

    {
      "2025": ["2025-02-26", "2025-03-14", "2025-04-10", "2025-04-18", ...],
      "2026": ["2026-01-26", "2026-02-17", "2026-03-04", ...]
    }

Year-keyed lookup keeps the file small + easy to scan. Comments are not
JSON-compatible; if you need annotations, use a sibling ``.md`` doc.

Operator note: the NSE circular for next-year holidays is typically
published in December of the prior year. Verify against the SEBI /
NSE-official "List of Trading Holidays" before relying on this for
financial computations.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path

from loguru import logger

# Resolve project root by walking up from this file. Anchoring on
# Path(__file__) means the loader works regardless of cwd.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_PATH = _PROJECT_ROOT / "database" / "nse_holidays.json"


def _resolve_source() -> Path | None:
    """Pick the holidays file to read, or None if neither exists."""
    env_path = os.getenv("LAABH_NSE_HOLIDAYS_FILE")
    if env_path:
        p = Path(env_path).expanduser()
        if p.exists():
            return p
        logger.warning(f"nse_holidays: LAABH_NSE_HOLIDAYS_FILE={p} not found")
        return None
    if _DEFAULT_PATH.exists():
        return _DEFAULT_PATH
    return None


def _load_all() -> frozenset[date]:
    """Parse the holidays JSON into a frozenset of dates.

    Reads the file on every call. The backfill scripts call this once at
    startup (so caching adds no value), and dropping the cache means
    operator edits to ``database/nse_holidays.json`` are picked up
    without a process restart. The file is small (<1 KB), the parse is
    microseconds.
    """
    source = _resolve_source()
    if source is None:
        logger.warning(
            "nse_holidays: no holidays file found "
            f"(checked LAABH_NSE_HOLIDAYS_FILE and {_DEFAULT_PATH}). "
            "Backfill scripts will treat every Mon-Fri as a trading day. "
            "Populate database/nse_holidays.json to enable holiday filtering."
        )
        return frozenset()

    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"nse_holidays: failed to parse {source}: {exc!r}")
        return frozenset()

    out: set[date] = set()
    for year_key, items in payload.items():
        # Skip non-year keys (README arrays, metadata, etc.) silently.
        # A real year key is 4 digits; everything else is human comment.
        if not (isinstance(year_key, str) and year_key.isdigit() and len(year_key) == 4):
            continue
        if not isinstance(items, list):
            continue
        for raw in items:
            try:
                out.add(datetime.strptime(str(raw), "%Y-%m-%d").date())
            except ValueError:
                logger.warning(
                    f"nse_holidays: skipping unparseable date {raw!r} "
                    f"under year {year_key} in {source}"
                )

    logger.info(f"nse_holidays: loaded {len(out)} holidays from {source}")
    return frozenset(out)


def load_nse_holidays(
    start: date | None = None,
    end: date | None = None,
) -> frozenset[date]:
    """Return the set of NSE holidays in the optional ``[start, end]`` window.

    No window → return the full loaded set. Useful for both the prereqs
    script (180-day backfill window) and the Dhan backfill (longer
    window).
    """
    all_holidays = _load_all()
    if start is None and end is None:
        return all_holidays
    lo = start or date.min
    hi = end or date.max
    return frozenset(d for d in all_holidays if lo <= d <= hi)
