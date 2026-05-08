"""Shared helpers for SQL-backed tool executors.

Three problems solved here:

1. **ID resolution.** Persona output schemas declare `instrument_id: integer`
   but every DB column is UUID. The LLM ends up passing either:
   * a UUID string ("749b202f-…"),
   * a symbol ("RELIANCE"),
   * or a fabricated integer ("234").
   `resolve_instrument_id` accepts all three and returns the canonical UUID
   string (or None when nothing matches).

2. **Datetime binding.** asyncpg refuses to bind ISO strings to TIMESTAMP/DATE
   columns; it requires real `datetime` / `date` objects. The LLM speaks ISO
   strings. `parse_dt` and `parse_date` convert defensively.

3. **Missing-table tolerance.** Several tools query `agent_predictions` /
   `agent_predictions_outcomes` which only exist after migration 0009. When
   the table is absent, `degrade_on_missing_table` lets the executor return
   a structured "not-yet-available" payload instead of raising.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ID resolution
# ---------------------------------------------------------------------------

async def resolve_instrument_id(db, raw: Any) -> str | None:
    """Resolve a UUID, symbol, or integer to the canonical instrument UUID.

    Returns None when no match is found — callers should treat that as
    "instrument not in DB" rather than retry.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None

    # Already a UUID? Validate and return.
    try:
        return str(UUID(s))
    except (ValueError, AttributeError):
        pass

    # Symbol lookup. Try exact match first, then case-insensitive.
    try:
        result = await db.execute(
            text("SELECT id FROM instruments WHERE symbol = :s LIMIT 1"),
            {"s": s},
        )
        row = result.fetchone()
        if row:
            return str(row[0])
        result = await db.execute(
            text("SELECT id FROM instruments WHERE UPPER(symbol) = UPPER(:s) LIMIT 1"),
            {"s": s},
        )
        row = result.fetchone()
        if row:
            return str(row[0])
    except Exception as e:
        log.warning("resolve_instrument_id symbol lookup failed: %s", e)

    return None


async def resolve_analyst_id(db, raw: Any) -> str | None:
    """Same idea as resolve_instrument_id, but for analysts.id (UUID column)."""
    if raw is None:
        return None
    s = str(raw).strip()
    try:
        return str(UUID(s))
    except (ValueError, AttributeError):
        pass
    try:
        result = await db.execute(
            text("SELECT id FROM analysts WHERE name ILIKE :s OR organization ILIKE :s LIMIT 1"),
            {"s": s},
        )
        row = result.fetchone()
        if row:
            return str(row[0])
    except Exception as e:
        log.warning("resolve_analyst_id lookup failed: %s", e)
    return None


# ---------------------------------------------------------------------------
# Datetime / date parsing
# ---------------------------------------------------------------------------

def parse_dt(raw: Any) -> datetime | None:
    """Parse a value into a `datetime`. Accepts datetime, ISO string, date, None."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, date):
        return datetime(raw.year, raw.month, raw.day)
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw))
    s = str(raw).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            log.warning("parse_dt could not parse %r", raw)
            return None


def parse_date(raw: Any) -> date | None:
    """Parse a value into a `date`. Accepts date, datetime, ISO string."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    s = str(raw).strip()
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            log.warning("parse_date could not parse %r", raw)
            return None


# ---------------------------------------------------------------------------
# Missing-table tolerance
# ---------------------------------------------------------------------------

_MISSING_TABLE_MARKERS = (
    "UndefinedTable", "does not exist", "relation",
)


def is_missing_table(exc: BaseException) -> bool:
    msg = str(exc)
    return any(m in msg for m in _MISSING_TABLE_MARKERS)
