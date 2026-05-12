"""F&O Universe Phase 1 filter — liquidity and OI screening.

Phase 1 runs pre-market (7:00 AM IST) after the chain snapshot is collected.
It scores every F&O instrument on three liquidity criteria and writes a
`fno_candidates` row with phase=1 for each instrument that passes.

Liquidity criteria (all must pass):
  1. ATM OI ≥ config.fno_phase1_min_atm_oi (default 2 000 Tier 1, 1 000 Tier 2)
     measured against the instrument's target expiry from next_weekly_expiry().
     An additional OI-collapse guard rejects instruments whose current ATM OI
     has dropped below fno_phase1_oi_collapse_pct (40%) of their 10-day rolling
     average — catches corporate-action / circuit-breaker illiquidity.
  2. ATM bid-ask spread ≤ config.fno_phase1_max_spread_pct (default 1.5%)
  3. 5-day average equity volume ≥ config.fno_phase1_min_avg_volume (default 500 000 shares)
     (if no volume data, criterion is skipped — not treated as a fail)

IV-ban exclusion: any symbol on today's F&O ban list is skipped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Sequence

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.config import Settings
from src.db import session_scope
from src.fno.ban_list import get_banned_ids
from src.fno.calendar import next_weekly_expiry
from src.models.fno_candidate import FNOCandidate
from src.models.fno_chain import OptionsChain
from src.models.instrument import Instrument
from src.models.price import PriceDaily

_settings = Settings()


# ---------------------------------------------------------------------------
# Pure data structures
# ---------------------------------------------------------------------------

@dataclass
class LiquidityResult:
    instrument_id: str
    symbol: str
    passed: bool
    atm_oi: int | None = None
    atm_spread_pct: float | None = None
    avg_volume_5d: int | None = None
    fail_reason: str | None = None


# ---------------------------------------------------------------------------
# Pure screening helpers (no I/O — unit-testable)
# ---------------------------------------------------------------------------

def check_atm_oi(atm_oi: int | None, min_oi: int) -> tuple[bool, str | None]:
    if atm_oi is None:
        return False, "no_atm_oi_data"
    if atm_oi < min_oi:
        return False, f"atm_oi={atm_oi}<{min_oi}"
    return True, None


def check_spread(atm_spread_pct: float | None, max_spread: float) -> tuple[bool, str | None]:
    if atm_spread_pct is None:
        return False, "no_spread_data"
    if atm_spread_pct > max_spread:
        return False, f"spread={atm_spread_pct:.2f}%>{max_spread}%"
    return True, None


def check_volume(avg_volume: int | None, min_volume: int) -> tuple[bool, str | None]:
    """Volume check — skipped (passes) if no data is available."""
    if avg_volume is None:
        return True, None  # not a hard fail
    if avg_volume < min_volume:
        return False, f"vol={avg_volume}<{min_volume}"
    return True, None


def apply_liquidity_filter(
    atm_oi: int | None,
    atm_spread_pct: float | None,
    avg_volume_5d: int | None,
    *,
    min_oi: int,
    max_spread_pct: float,
    min_volume: int,
) -> tuple[bool, str | None]:
    """Return (passed, fail_reason). fail_reason is None if passed."""
    ok, reason = check_atm_oi(atm_oi, min_oi)
    if not ok:
        return False, reason
    ok, reason = check_spread(atm_spread_pct, max_spread_pct)
    if not ok:
        return False, reason
    ok, reason = check_volume(avg_volume_5d, min_volume)
    if not ok:
        return False, reason
    return True, None


def compute_atm_spread_pct(
    bid: float | None,
    ask: float | None,
    mid: float | None,
) -> float | None:
    """Bid-ask spread as a decimal fraction of mid price (e.g. 0.005 = 0.5%)."""
    if bid is None or ask is None or mid is None or mid == 0:
        return None
    return round((ask - bid) / mid, 6)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _get_fno_instruments(session) -> list[tuple[str, str]]:
    result = await session.execute(
        select(Instrument.id, Instrument.symbol).where(
            Instrument.is_fno == True,  # noqa: E712
            Instrument.is_active == True,
        )
    )
    return [(str(r.id), r.symbol) for r in result.all()]


async def _get_atm_chain_row(
    session,
    instrument_id: str,
    *,
    as_of: datetime | None = None,
    expiry_date: date | None = None,
) -> tuple[int | None, float | None]:
    """Return (atm_oi, atm_spread_pct) from the latest chain snapshot for the
    instrument, optionally bounded to a specific expiry and timestamp.

    ``expiry_date`` should be the result of ``next_weekly_expiry(symbol, run_date)``
    so the OI measurement covers only the expiry that Phase 3 will actually trade,
    not a stale far-month contract that inflates or deflates the ATM figure.
    """
    where_clauses = [OptionsChain.instrument_id == instrument_id]
    if as_of is not None:
        where_clauses.append(OptionsChain.snapshot_at <= as_of)
    if expiry_date is not None:
        where_clauses.append(OptionsChain.expiry_date == expiry_date)
    snap_subq = (
        select(func.max(OptionsChain.snapshot_at))
        .where(*where_clauses)
        .scalar_subquery()
    )

    main_where = [
        OptionsChain.instrument_id == instrument_id,
        OptionsChain.snapshot_at == snap_subq,
    ]
    if expiry_date is not None:
        main_where.append(OptionsChain.expiry_date == expiry_date)

    rows = await session.execute(
        select(
            OptionsChain.option_type,
            OptionsChain.strike_price,
            OptionsChain.oi,
            OptionsChain.bid_price,
            OptionsChain.ask_price,
            OptionsChain.underlying_ltp,
        ).where(*main_where)
    )
    rows = rows.all()
    if not rows:
        return None, None

    underlying = float(rows[0].underlying_ltp or 0)
    if underlying == 0:
        return None, None

    strikes = sorted({float(r.strike_price) for r in rows})
    atm_strike = min(strikes, key=lambda s: abs(s - underlying))

    # Sum OI for ATM strike across CE + PE
    atm_oi = sum(
        r.oi or 0
        for r in rows
        if abs(float(r.strike_price) - atm_strike) < 0.01
    )

    # Compute spread from ATM CE or PE (prefer CE)
    atm_rows = [r for r in rows if abs(float(r.strike_price) - atm_strike) < 0.01]
    spread = None
    for r in atm_rows:
        bid = float(r.bid_price) if r.bid_price else None
        ask = float(r.ask_price) if r.ask_price else None
        if bid and ask:
            mid = (bid + ask) / 2
            spread = compute_atm_spread_pct(bid, ask, mid)
            break

    return (atm_oi if atm_oi > 0 else None), spread


async def _get_avg_volume_5d(
    session,
    instrument_id: str,
    as_of: date,
    *,
    cutoff_date: date | None = None,
) -> int | None:
    """Return 5-day average daily volume from price_daily ending before cutoff_date."""
    cutoff = cutoff_date if cutoff_date is not None else as_of
    # Pick the 5 most recent rows (subquery), then average their volume.
    # A single SELECT with both AVG and ORDER BY+LIMIT is invalid SQL.
    recent = (
        select(PriceDaily.volume)
        .where(
            PriceDaily.instrument_id == instrument_id,
            PriceDaily.date < cutoff,
        )
        .order_by(PriceDaily.date.desc())
        .limit(5)
        .subquery()
    )
    result = await session.execute(select(func.avg(recent.c.volume)))
    avg = result.scalar_one_or_none()
    return int(avg) if avg else None


async def _upsert_candidate(
    session,
    instrument_id: str,
    run_date: date,
    passed: bool,
    atm_oi: int | None,
    atm_spread_pct: float | None,
    avg_volume_5d: int | None,
    config_version: str,
) -> None:
    stmt = pg_insert(FNOCandidate).values(
        instrument_id=instrument_id,
        run_date=run_date,
        phase=1,
        passed_liquidity=passed,
        atm_oi=atm_oi,
        atm_spread_pct=Decimal(str(atm_spread_pct)) if atm_spread_pct is not None else None,
        avg_volume_5d=avg_volume_5d,
        config_version=config_version,
        created_at=datetime.now(tz=timezone.utc),
    ).on_conflict_do_update(
        index_elements=["instrument_id", "run_date", "phase"],
        set_={
            "passed_liquidity": passed,
            "atm_oi": atm_oi,
            "atm_spread_pct": Decimal(str(atm_spread_pct)) if atm_spread_pct is not None else None,
            "avg_volume_5d": avg_volume_5d,
            "config_version": config_version,
        }
    )
    await session.execute(stmt)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_phase1(
    run_date: date | None = None,
    *,
    as_of: datetime | None = None,
) -> list[LiquidityResult]:
    """Run Phase 1 liquidity filter for all F&O instruments.

    Returns a list of LiquidityResult for every instrument screened.
    Passing instruments get a phase=1 fno_candidates row written.
    When `as_of` is set, chain queries use the snapshot on-or-before that timestamp.
    """
    if run_date is None:
        run_date = date.today() if as_of is None else as_of.date()

    cfg = _settings
    min_oi_tier1 = cfg.fno_phase1_min_atm_oi
    min_oi_tier2 = cfg.fno_phase1_min_atm_oi_tier2
    max_spread_tier1 = cfg.fno_phase1_max_atm_spread_pct_tier1
    max_spread_tier2 = cfg.fno_phase1_max_atm_spread_pct
    min_vol = cfg.fno_phase1_min_avg_volume_5d
    config_ver = getattr(cfg, "fno_ranker_version", "v1")

    async with session_scope() as session:
        instruments = await _get_fno_instruments(session)
        # Tier lookup so Phase 1 can apply per-tier OI thresholds.
        from src.models.fno_collection_tier import FNOCollectionTier
        tier_rows = await session.execute(
            select(FNOCollectionTier.instrument_id, FNOCollectionTier.tier)
        )
        tier_by_id = {str(r.instrument_id): r.tier for r in tier_rows.all()}

        # Rolling-average ATM OI per instrument: look back 14 calendar days
        # (≈ 10 trading days) using Phase-1 history already written to
        # fno_candidates.  Only instruments with ≥ fno_phase1_oi_collapse_min_days
        # of history get the collapse guard — brand-new passers are skipped.
        hist_rows = await session.execute(
            select(
                FNOCandidate.instrument_id,
                func.avg(FNOCandidate.atm_oi).label("avg_oi"),
            )
            .where(
                FNOCandidate.phase == 1,
                FNOCandidate.run_date >= run_date - timedelta(days=14),
                FNOCandidate.run_date < run_date,
                FNOCandidate.atm_oi.isnot(None),
                FNOCandidate.dryrun_run_id.is_(None),
            )
            .group_by(FNOCandidate.instrument_id)
            .having(func.count(FNOCandidate.atm_oi) >= cfg.fno_phase1_oi_collapse_min_days)
        )
        rolling_avg_oi: dict[str, float] = {
            str(r.instrument_id): float(r.avg_oi)
            for r in hist_rows.all()
            if r.avg_oi is not None
        }

    if not instruments:
        logger.warning("fno.universe: no F&O instruments found")
        return []

    banned_ids = await get_banned_ids()
    collapse_pct = cfg.fno_phase1_oi_collapse_pct

    results: list[LiquidityResult] = []

    for inst_id, symbol in instruments:
        if inst_id in banned_ids:
            logger.info(f"fno.universe: {symbol} skipped — on F&O ban list")
            continue

        try:
            # Target expiry for this instrument — Phase 1 measures OI against
            # the same contract that Phase 3 will propose trading.
            target_expiry = next_weekly_expiry(symbol, run_date)

            async with session_scope() as session:
                atm_oi, spread = await _get_atm_chain_row(
                    session, inst_id, as_of=as_of, expiry_date=target_expiry
                )
                avg_vol = await _get_avg_volume_5d(session, inst_id, run_date, cutoff_date=run_date)

            tier = tier_by_id.get(inst_id, 2)
            min_oi = min_oi_tier1 if tier == 1 else min_oi_tier2
            max_spread = max_spread_tier1 if tier == 1 else max_spread_tier2

            passed, fail_reason = apply_liquidity_filter(
                atm_oi, spread, avg_vol,
                min_oi=min_oi, max_spread_pct=max_spread, min_volume=min_vol,
            )

            # OI-collapse guard: reject if today's ATM OI has dropped below
            # collapse_pct of the instrument's own rolling average.  Only fires
            # when the instrument has enough Phase-1 history; new passers are
            # admitted unconditionally so they can start building history.
            if passed and atm_oi is not None:
                rolling_avg = rolling_avg_oi.get(inst_id)
                if rolling_avg is not None and atm_oi < collapse_pct * rolling_avg:
                    passed = False
                    fail_reason = (
                        f"oi_collapse:{atm_oi}<"
                        f"{collapse_pct*100:.0f}%_of_10d_avg_{rolling_avg:.0f}"
                    )

            res = LiquidityResult(
                instrument_id=inst_id,
                symbol=symbol,
                passed=passed,
                atm_oi=atm_oi,
                atm_spread_pct=spread,
                avg_volume_5d=avg_vol,
                fail_reason=fail_reason,
            )
            results.append(res)

            if passed:
                async with session_scope() as session:
                    await _upsert_candidate(
                        session, inst_id, run_date, True,
                        atm_oi, spread, avg_vol, config_ver,
                    )
                logger.debug(f"fno.universe: {symbol} PASS oi={atm_oi} spread={spread}")
            else:
                logger.debug(f"fno.universe: {symbol} FAIL {fail_reason}")

        except Exception as exc:
            logger.warning(f"fno.universe: {symbol} error: {exc}")

    passed_count = sum(1 for r in results if r.passed)
    logger.info(
        f"fno.universe: Phase 1 complete — {passed_count}/{len(results)} passed for {run_date}"
    )
    return results
