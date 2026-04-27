"""F&O Universe Phase 1 filter — liquidity and OI screening.

Phase 1 runs pre-market (7:00 AM IST) after the chain snapshot is collected.
It scores every F&O instrument on three liquidity criteria and writes a
`fno_candidates` row with phase=1 for each instrument that passes.

Liquidity criteria (all three must pass):
  1. ATM OI ≥ config.fno_phase1_min_atm_oi (default 50 000 contracts)
  2. ATM bid-ask spread ≤ config.fno_phase1_max_spread_pct (default 1.5%)
  3. 5-day average equity volume ≥ config.fno_phase1_min_avg_volume (default 500 000 shares)
     (if no volume data, criterion is skipped — not treated as a fail)

IV-ban exclusion: any symbol on today's F&O ban list is skipped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Sequence

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.config import Settings
from src.db import session_scope
from src.fno.ban_list import get_banned_ids
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
) -> tuple[int | None, float | None]:
    """Return (atm_oi, atm_spread_pct) from latest chain snapshot."""
    snap_subq = (
        select(func.max(OptionsChain.snapshot_at))
        .where(OptionsChain.instrument_id == instrument_id)
        .scalar_subquery()
    )

    rows = await session.execute(
        select(
            OptionsChain.option_type,
            OptionsChain.strike_price,
            OptionsChain.oi,
            OptionsChain.bid_price,
            OptionsChain.ask_price,
            OptionsChain.underlying_ltp,
        ).where(
            OptionsChain.instrument_id == instrument_id,
            OptionsChain.snapshot_at == snap_subq,
        )
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


async def _get_avg_volume_5d(session, instrument_id: str, as_of: date) -> int | None:
    """Return 5-day average daily volume from price_daily."""
    result = await session.execute(
        select(func.avg(PriceDaily.volume)).where(
            PriceDaily.instrument_id == instrument_id,
            PriceDaily.date < as_of,
        ).order_by(PriceDaily.date.desc()).limit(5)
    )
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

async def run_phase1(run_date: date | None = None) -> list[LiquidityResult]:
    """Run Phase 1 liquidity filter for all F&O instruments.

    Returns a list of LiquidityResult for every instrument screened.
    Passing instruments get a phase=1 fno_candidates row written.
    """
    if run_date is None:
        run_date = date.today()

    cfg = _settings
    min_oi = cfg.fno_phase1_min_atm_oi
    max_spread = cfg.fno_phase1_max_atm_spread_pct
    min_vol = cfg.fno_phase1_min_avg_volume_5d
    config_ver = getattr(cfg, "fno_ranker_version", "v1")

    async with session_scope() as session:
        instruments = await _get_fno_instruments(session)

    if not instruments:
        logger.warning("fno.universe: no F&O instruments found")
        return []

    banned_ids = await get_banned_ids()

    results: list[LiquidityResult] = []

    for inst_id, symbol in instruments:
        if inst_id in banned_ids:
            logger.info(f"fno.universe: {symbol} skipped — on F&O ban list")
            continue

        try:
            async with session_scope() as session:
                atm_oi, spread = await _get_atm_chain_row(session, inst_id)
                avg_vol = await _get_avg_volume_5d(session, inst_id, run_date)

            passed, fail_reason = apply_liquidity_filter(
                atm_oi, spread, avg_vol,
                min_oi=min_oi, max_spread_pct=max_spread, min_volume=min_vol,
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
