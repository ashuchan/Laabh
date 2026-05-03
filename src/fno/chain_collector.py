"""Options chain collector — NSE primary, Dhan fallback.

Angel One is NOT used for option chain ingestion (it has no option chain
endpoint and its WebSocket cap of 3,000 tokens is 8× below what the full
F&O universe requires).  Angel One continues to be used for underlying
ticks, India VIX, and the per-strike Greeks API.

Failover sequence per underlying per poll:
  1. Try NSE → on success, write options_chain row with source='nse'.
  2. On NSE failure → try Dhan → on success, write with source='dhan'.
  3. On Dhan failure → log a 'missed' outcome, populate chain_collection_issues
     for schema-mismatch errors.
"""
from __future__ import annotations

import contextlib
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from decimal import Decimal

from loguru import logger
from sqlalchemy import select, update

from src.config import get_settings
from src.db import session_scope
from src.fno.calendar import next_weekly_expiry
from src.fno.chain_parser import ChainRow, ChainSnapshot, enrich_chain_row
from src.fno.sources.base import ChainSnapshot as SourceSnapshot
from src.fno.sources.dhan_source import DhanSource
from src.fno.sources.exceptions import (
    AuthError,
    RateLimitError,
    SchemaError,
    SourceUnavailableError,
)
from src.fno.sources.nse_source import NSESource
from src.models.fno_chain import OptionsChain
from src.models.fno_chain_issue import ChainCollectionIssue
from src.models.fno_chain_log import ChainCollectionLog
from src.models.fno_collection_tier import FNOCollectionTier
from src.models.fno_source_health import SourceHealth
from src.models.instrument import Instrument

_settings = get_settings()

# Module-level source instances (one each — re-used across calls)
_nse: NSESource = NSESource()
_dhan: DhanSource = DhanSource()

# Context-variable override — set to a custom source inside replay_chain_source()
_ACTIVE_SOURCE: ContextVar = ContextVar("chain_active_source", default=None)


def _get_active_source():
    """Return the context-local source override, or None to use the live failover logic."""
    return _ACTIVE_SOURCE.get()


@contextlib.contextmanager
def replay_chain_source(source):
    """Context manager: override the primary chain source for the current async context."""
    token = _ACTIVE_SOURCE.set(source)
    try:
        yield
    finally:
        _ACTIVE_SOURCE.reset(token)


# ---------------------------------------------------------------------------
# Source health helpers
# ---------------------------------------------------------------------------

async def _record_source_success(source: str) -> None:
    now = datetime.now(tz=timezone.utc)
    async with session_scope() as session:
        await session.execute(
            update(SourceHealth)
            .where(SourceHealth.source == source)
            .values(
                status="healthy",
                consecutive_errors=0,
                last_success_at=now,
                updated_at=now,
            )
        )


async def _record_source_error(source: str, error: str) -> None:
    """Increment consecutive errors; degrade source after policy threshold."""
    now = datetime.now(tz=timezone.utc)
    async with session_scope() as session:
        result = await session.execute(
            select(SourceHealth).where(SourceHealth.source == source)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return
        row.consecutive_errors = (row.consecutive_errors or 0) + 1
        row.last_error_at = now
        row.last_error = error[:500]
        row.updated_at = now
        if row.consecutive_errors >= _settings.fno_source_degrade_after_consecutive_errors:
            row.status = "degraded"


async def _record_schema_mismatch(
    source: str,
    instrument: Instrument,
    error: str,
    raw: str,
) -> None:
    """Log a schema mismatch issue; degrade the source after N consecutive mismatches."""
    now = datetime.now(tz=timezone.utc)
    async with session_scope() as session:
        session.add(
            ChainCollectionIssue(
                id=uuid.uuid4(),
                source=source,
                instrument_id=instrument.id,
                issue_type="schema_mismatch",
                error_message=error,
                raw_response=raw[:8192],
                detected_at=now,
            )
        )

        # Count consecutive schema mismatches for this source (last 24h, unresolved)
        from datetime import timedelta
        cutoff = now - timedelta(hours=24)
        result = await session.execute(
            select(ChainCollectionIssue).where(
                ChainCollectionIssue.source == source,
                ChainCollectionIssue.issue_type == "schema_mismatch",
                ChainCollectionIssue.detected_at >= cutoff,
                ChainCollectionIssue.resolved_at.is_(None),
            )
        )
        recent_count = len(result.scalars().all())

        threshold = _settings.fno_source_degrade_after_schema_errors
        if recent_count >= threshold:
            await session.execute(
                update(SourceHealth)
                .where(SourceHealth.source == source)
                .values(status="degraded", updated_at=now)
            )
            logger.warning(
                f"chain_collector: {source} degraded after {recent_count} schema mismatches"
            )


# ---------------------------------------------------------------------------
# Persistence helper
# ---------------------------------------------------------------------------

async def _persist_snapshot(
    snapshot: SourceSnapshot,
    instrument: Instrument,
    source: str,
    *,
    as_of: datetime | None = None,
    dryrun_run_id: "uuid.UUID | None" = None,
) -> None:
    """Convert a SourceSnapshot to ChainRows, enrich Greeks, write to DB."""
    now = as_of if as_of is not None else snapshot.snapshot_at
    r_factor = _settings.fno_risk_free_rate_pct / 100.0

    async with session_scope() as session:
        for strike in snapshot.strikes:
            T = max(
                0.0,
                (snapshot.expiry_date - now.date()).days / 365.0,
            )
            underlying = float(snapshot.underlying_ltp or 0)
            K = float(strike.strike)
            opt = strike.option_type

            row = ChainRow(
                instrument_id=instrument.id,
                expiry_date=snapshot.expiry_date,
                strike_price=strike.strike,
                option_type=opt,
                ltp=strike.ltp,
                bid_price=strike.bid,
                ask_price=strike.ask,
                bid_qty=strike.bid_qty,
                ask_qty=strike.ask_qty,
                volume=strike.volume,
                oi=strike.oi,
                iv=strike.iv,
                delta=strike.delta,
                gamma=strike.gamma,
                theta=strike.theta,
                vega=strike.vega,
                underlying_ltp=snapshot.underlying_ltp,
            )

            # Compute Greeks when the source (NSE) didn't supply them
            if underlying > 0 and K > 0 and T > 0:
                if row.iv is None or row.delta is None:
                    row = enrich_chain_row(row, T, r=r_factor)

            session.add(
                OptionsChain(
                    instrument_id=row.instrument_id,
                    snapshot_at=now,
                    expiry_date=row.expiry_date,
                    strike_price=row.strike_price,
                    option_type=row.option_type,
                    ltp=row.ltp,
                    bid_price=row.bid_price,
                    ask_price=row.ask_price,
                    bid_qty=row.bid_qty,
                    ask_qty=row.ask_qty,
                    volume=row.volume,
                    oi=row.oi,
                    oi_change=None,
                    iv=row.iv,
                    delta=row.delta,
                    gamma=row.gamma,
                    theta=row.theta,
                    vega=row.vega,
                    underlying_ltp=row.underlying_ltp,
                    source=source,
                    dryrun_run_id=dryrun_run_id,
                )
            )


# ---------------------------------------------------------------------------
# Per-underlying collection with failover
# ---------------------------------------------------------------------------

async def collect_one(
    instrument: Instrument,
    *,
    as_of: datetime | None = None,
    dryrun_run_id: "uuid.UUID | None" = None,
) -> None:
    """Collect and persist chain data for one instrument (NSE → Dhan failover)."""
    started = as_of if as_of is not None else datetime.now(tz=timezone.utc)
    expiry = next_weekly_expiry(instrument.symbol, reference=started.date())

    log = ChainCollectionLog(
        id=uuid.uuid4(),
        instrument_id=instrument.id,
        attempted_at=started,
        primary_source="nse",
    )

    nse_ok = False
    dhan_ok = False

    # Allow caller to inject a custom source (used in replay mode)
    primary_source = _get_active_source() or _nse

    # --- Primary source ---
    primary_name = getattr(primary_source, "name", "nse")
    log.primary_source = primary_name
    try:
        snapshot = await primary_source.fetch(instrument.symbol, expiry)
        await _persist_snapshot(snapshot, instrument, source=primary_name, as_of=as_of, dryrun_run_id=dryrun_run_id)
        log.final_source = primary_name
        log.status = "ok"
        nse_ok = True
        if as_of is None:
            await _record_source_success(primary_name)
        logger.info(f"chain_collector: {instrument.symbol} OK via {primary_name}")
    except SchemaError as exc:
        log.nse_error = f"schema: {exc}"
        if as_of is None:
            await _record_schema_mismatch(primary_name, instrument, str(exc), exc.raw_response)
            await _record_source_error(primary_name, str(exc))
    except (RateLimitError, AuthError, SourceUnavailableError) as exc:
        log.nse_error = str(exc)
        if as_of is None:
            await _record_source_error(primary_name, str(exc))
        logger.warning(f"chain_collector: {instrument.symbol} {primary_name} failed: {exc}")

    # --- Fallback: Dhan (only when primary failed and we're in live mode) ---
    if not nse_ok and as_of is None:
        log.fallback_source = "dhan"
        try:
            snapshot = await _dhan.fetch(instrument.symbol, expiry)
            await _persist_snapshot(snapshot, instrument, source="dhan", dryrun_run_id=dryrun_run_id)
            log.final_source = "dhan"
            log.status = "fallback_used"
            dhan_ok = True
            await _record_source_success("dhan")
            logger.info(f"chain_collector: {instrument.symbol} OK via Dhan (fallback)")
        except SchemaError as exc:
            log.dhan_error = f"schema: {exc}"
            await _record_schema_mismatch("dhan", instrument, str(exc), exc.raw_response)
            await _record_source_error("dhan", str(exc))
        except (RateLimitError, AuthError, SourceUnavailableError) as exc:
            log.dhan_error = str(exc)
            await _record_source_error("dhan", str(exc))
            logger.error(
                f"chain_collector: {instrument.symbol} MISSED — {primary_name}: {log.nse_error}, "
                f"Dhan: {exc}"
            )

    if not nse_ok and not dhan_ok:
        log.status = "missed"

    elapsed_ms = int((datetime.now(tz=timezone.utc) - started).total_seconds() * 1000)
    log.latency_ms = elapsed_ms
    log.dryrun_run_id = dryrun_run_id

    async with session_scope() as session:
        session.add(log)


# ---------------------------------------------------------------------------
# Tier-aware collection
# ---------------------------------------------------------------------------

async def collect_tier(
    tier: int,
    *,
    as_of: datetime | None = None,
    dryrun_run_id: "uuid.UUID | None" = None,
) -> None:
    """Collect chain data for all instruments in the given tier (1 or 2)."""
    async with session_scope() as session:
        result = await session.execute(
            select(Instrument)
            .join(
                FNOCollectionTier,
                FNOCollectionTier.instrument_id == Instrument.id,
            )
            .where(
                FNOCollectionTier.tier == tier,
                Instrument.is_fno == True,  # noqa: E712
                Instrument.is_active == True,  # noqa: E712
            )
        )
        instruments = result.scalars().all()

    if not instruments:
        logger.info(
            f"chain_collector: no tier-{tier} instruments (tier table may be empty)"
        )
        return

    logger.info(
        f"chain_collector: tier-{tier} sweep for {len(instruments)} instruments"
    )
    for inst in instruments:
        await collect_one(inst, as_of=as_of, dryrun_run_id=dryrun_run_id)


async def collect_all(
    *,
    as_of: datetime | None = None,
    dryrun_run_id: "uuid.UUID | None" = None,
) -> None:
    """Fallback: collect chains for all F&O instruments regardless of tier."""
    async with session_scope() as session:
        result = await session.execute(
            select(Instrument).where(
                Instrument.is_fno == True,  # noqa: E712
                Instrument.is_active == True,  # noqa: E712
            )
        )
        instruments = result.scalars().all()

    logger.info(
        f"chain_collector: full sweep for {len(instruments)} F&O instruments"
    )
    for inst in instruments:
        await collect_one(inst, as_of=as_of, dryrun_run_id=dryrun_run_id)
