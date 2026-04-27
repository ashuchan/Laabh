"""IV history builder — computes and persists daily ATM IV with IV Rank / IV Percentile.

Runs once per day after market close (3:45 PM IST).

Algorithm:
  1. For each F&O instrument, find the nearest ATM CE and PE from the latest
     options_chain snapshot of the day.
  2. Average their IVs → atm_iv for today.
  3. Load the last 52 weeks of atm_iv rows for that instrument.
  4. Compute IV Rank  = (today_iv - min_52w) / (max_52w - min_52w) * 100
     Compute IV Percentile = fraction of 52w days where iv < today_iv * 100
  5. Upsert into iv_history (idempotent — re-running same day overwrites).
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Sequence

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.db import session_scope
from src.models.fno_chain import OptionsChain
from src.models.fno_iv import IVHistory
from src.models.instrument import Instrument


# ---------------------------------------------------------------------------
# Pure computation helpers (no I/O — easily unit-tested)
# ---------------------------------------------------------------------------

def compute_iv_rank(current_iv: float, history: Sequence[float]) -> float | None:
    """IV Rank: position of current IV within 52-week high/low range (0-100)."""
    if not history:
        return None
    lo, hi = min(history), max(history)
    if hi == lo:
        return 50.0
    return round((current_iv - lo) / (hi - lo) * 100, 2)


def compute_iv_percentile(current_iv: float, history: Sequence[float]) -> float | None:
    """IV Percentile: % of days in history where IV was below current IV (0-100)."""
    if not history:
        return None
    below = sum(1 for v in history if v < current_iv)
    return round(below / len(history) * 100, 2)


def select_atm_iv(
    chain_rows: Sequence[tuple[str, float, float]],
    underlying_price: float,
) -> float | None:
    """Given (option_type, strike, iv) tuples, return ATM IV averaged over CE+PE.

    Picks the strike closest to underlying_price, then averages the CE and PE IVs
    at that strike (uses whichever side is available if only one is present).
    """
    if not chain_rows:
        return None

    strikes = sorted({strike for _, strike, _ in chain_rows})
    if not strikes:
        return None

    atm_strike = min(strikes, key=lambda s: abs(s - underlying_price))

    ivs = [iv for opt, strike, iv in chain_rows if strike == atm_strike and iv is not None]
    if not ivs:
        return None
    return round(sum(ivs) / len(ivs), 4)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _get_atm_iv_from_chain(
    session,
    instrument_id: str,
    target_date: date,
) -> float | None:
    """Query options_chain for today's ATM IV for an instrument."""
    # Latest snapshot_at during target_date
    snap_subq = (
        select(func.max(OptionsChain.snapshot_at))
        .where(
            OptionsChain.instrument_id == instrument_id,
            func.date(OptionsChain.snapshot_at) == target_date,
        )
        .scalar_subquery()
    )

    rows = await session.execute(
        select(
            OptionsChain.option_type,
            OptionsChain.strike_price,
            OptionsChain.iv,
            OptionsChain.underlying_ltp,
        ).where(
            OptionsChain.instrument_id == instrument_id,
            OptionsChain.snapshot_at == snap_subq,
            OptionsChain.iv.isnot(None),
        )
    )
    rows = rows.all()
    if not rows:
        return None

    underlying_price = float(rows[0].underlying_ltp or 0)
    if underlying_price == 0:
        return None

    chain_tuples = [
        (r.option_type, float(r.strike_price), float(r.iv))
        for r in rows
    ]
    return select_atm_iv(chain_tuples, underlying_price)


async def _get_52w_history(
    session,
    instrument_id: str,
    before_date: date,
) -> list[float]:
    """Return up to 52 weeks of atm_iv values before (not including) before_date."""
    cutoff = before_date - timedelta(weeks=52)
    result = await session.execute(
        select(IVHistory.atm_iv).where(
            IVHistory.instrument_id == instrument_id,
            IVHistory.date >= cutoff,
            IVHistory.date < before_date,
        ).order_by(IVHistory.date)
    )
    return [float(r.atm_iv) for r in result.all()]


async def _upsert_iv_row(
    session,
    instrument_id: str,
    target_date: date,
    atm_iv: float,
    iv_rank: float | None,
    iv_pct: float | None,
) -> None:
    stmt = pg_insert(IVHistory).values(
        instrument_id=instrument_id,
        date=target_date,
        atm_iv=Decimal(str(round(atm_iv, 4))),
        iv_rank_52w=Decimal(str(iv_rank)) if iv_rank is not None else None,
        iv_percentile_52w=Decimal(str(iv_pct)) if iv_pct is not None else None,
    ).on_conflict_do_update(
        index_elements=["instrument_id", "date"],
        set_={
            "atm_iv": Decimal(str(round(atm_iv, 4))),
            "iv_rank_52w": Decimal(str(iv_rank)) if iv_rank is not None else None,
            "iv_percentile_52w": Decimal(str(iv_pct)) if iv_pct is not None else None,
        }
    )
    await session.execute(stmt)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def build_for_date(target_date: date | None = None) -> int:
    """Compute and upsert ATM IV + rank/percentile for all F&O instruments.

    Returns the count of rows upserted.
    """
    if target_date is None:
        target_date = date.today()

    async with session_scope() as session:
        result = await session.execute(
            select(Instrument.id, Instrument.symbol).where(
                Instrument.is_fno == True,  # noqa: E712
                Instrument.is_active == True,
            )
        )
        instruments = result.all()

    if not instruments:
        logger.warning("iv_history_builder: no F&O instruments found")
        return 0

    upserted = 0
    for inst_id, symbol in instruments:
        try:
            async with session_scope() as session:
                atm_iv = await _get_atm_iv_from_chain(session, str(inst_id), target_date)

            if atm_iv is None:
                logger.debug(f"iv_history_builder: no chain data for {symbol} on {target_date}")
                continue

            async with session_scope() as session:
                history = await _get_52w_history(session, str(inst_id), target_date)

            iv_rank = compute_iv_rank(atm_iv, history)
            iv_pct = compute_iv_percentile(atm_iv, history)

            async with session_scope() as session:
                await _upsert_iv_row(session, str(inst_id), target_date, atm_iv, iv_rank, iv_pct)

            upserted += 1
            logger.debug(
                f"iv_history_builder: {symbol} atm_iv={atm_iv:.2f}% "
                f"rank={iv_rank} pct={iv_pct}"
            )

        except Exception as exc:
            logger.warning(f"iv_history_builder: {symbol} failed: {exc}")

    logger.info(f"iv_history_builder: upserted {upserted}/{len(instruments)} rows for {target_date}")
    return upserted
