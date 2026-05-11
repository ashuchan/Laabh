"""Intraday universe expansion via live top-movers scanning.

Reads today's intraday bars from ``price_intraday`` and yesterday's close from
``price_daily`` for all active F&O instruments, computes the live pct_change
from previous close, and produces ranked admission/eviction pairs for the
orchestrator's restless-bandit arm replacement loop.

The scanner is intentionally side-effect-free — it queries the DB and returns
data structures; all mutations (selector, universe list) happen in the
orchestrator's main loop under cooperative asyncio exclusion.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Optional

import pytz
from loguru import logger
from sqlalchemy import and_, func, select

from src.config import get_settings
from src.db import session_scope
from src.models.fno_ban import FNOBanList
from src.models.instrument import Instrument
from src.models.price import PriceDaily
from src.models.price_intraday import PriceIntraday

if TYPE_CHECKING:
    from src.quant.bandit.selector import ArmSelector

_IST = pytz.timezone("Asia/Kolkata")


@dataclass(frozen=True)
class LiveMover:
    """One F&O underlying's current intraday move."""

    id: uuid.UUID
    symbol: str
    name: str
    prev_close: float
    current_price: float
    pct_change: float           # (current - prev) / prev × 100
    avg_volume_5d: int


@dataclass(frozen=True)
class ReplacementPair:
    """One (evict, admit) replacement decision for the orchestrator."""

    evict_symbol: str           # arm symbol to remove from active universe
    admit_instrument: dict      # {id, symbol, name} shape (same as universe entries)
    admit_momentum_pct: float   # absolute momentum driving admission
    evict_momentum_pct: float   # absolute momentum of the evicted arm (for logging)


class LiveGainersScanner:
    """Scans live ``price_intraday`` data to find F&O instruments with strong
    intraday momentum that are not currently in the active universe.

    Designed for use in ``LAABH_INTRADAY_MODE=quant``. One instance is created
    per orchestrator session and reused across scan intervals.
    """

    def __init__(self) -> None:
        self._settings = get_settings()

    # ------------------------------------------------------------------
    # Primary entry point called by the orchestrator
    # ------------------------------------------------------------------

    async def compute_replacements(
        self,
        active_universe: list[dict],
        selector: "ArmSelector",
        open_position_symbols: set[str],
        *,
        trading_date: date,
        primitives_list: list[str],
        as_of: datetime | None = None,
        dryrun_run_id: uuid.UUID | None = None,
    ) -> list[ReplacementPair]:
        """Return (evict, admit) pairs for this scan cycle.

        Args:
            open_position_symbols: Set of underlying symbols (not arm IDs) with
                open positions. Pre-computed by the caller from ``arm_meta`` to
                avoid fragile arm_id string splitting here (symbols may contain
                underscores, e.g. ``BAJAJ_AUTO``).

        Guarantees:
        - No pair evicts an arm with an open position.
        - Eviction target must have >= min_pulls_before_evict bandit pulls.
        - Candidate must beat eviction target by at least hysteresis_pct.
        - At most max_replacements pairs returned.
        - Pairs are sorted by (admit_momentum - evict_momentum) descending.
        """
        s = self._settings
        now_ist = datetime.now(tz=_IST)
        stop = now_ist.replace(
            hour=s.laabh_quant_intraday_scanner_stop_hour,
            minute=s.laabh_quant_intraday_scanner_stop_minute,
            second=0,
            microsecond=0,
        )
        if now_ist >= stop:
            logger.info("[SCANNER] Past stop time — no expansion")
            return []

        active_symbols = {u["symbol"] for u in active_universe}
        ban_set = await self._load_ban_set(trading_date)

        # Live movers across ALL F&O instruments
        all_movers = await self._fetch_live_movers(trading_date, ban_set)
        if not all_movers:
            logger.debug("[SCANNER] No live movers available yet")
            return []

        movers_by_symbol = {m.symbol: m for m in all_movers}

        # Candidates: not already active, momentum >= threshold
        candidates = [
            m for m in all_movers
            if m.symbol not in active_symbols
            and abs(m.pct_change) >= s.laabh_quant_intraday_scanner_min_momentum_pct
        ]
        if not candidates:
            logger.debug("[SCANNER] No candidates exceed momentum threshold")
            return []

        # Eviction pool: active arms, no open position, min pulls met
        eviction_pool: list[tuple[str, float]] = []  # (symbol, |pct_change|)
        for u in active_universe:
            sym = u["symbol"]
            if sym in open_position_symbols:
                continue
            # Check min pulls across ALL primitives for this symbol
            arm_ids = [f"{sym}_{p}" for p in primitives_list]
            total_pulls = sum(selector.n_obs(a) for a in arm_ids)
            min_pulls = s.laabh_quant_intraday_scanner_min_pulls_before_evict * len(primitives_list)
            if total_pulls < min_pulls:
                continue
            mover = movers_by_symbol.get(sym)
            momentum = abs(mover.pct_change) if mover else 0.0
            eviction_pool.append((sym, momentum))

        if not eviction_pool:
            logger.debug("[SCANNER] No eviction candidates (min pulls or open positions)")
            return []

        # Sort eviction pool: lowest momentum first (these are the weakest)
        eviction_pool.sort(key=lambda x: x[1])

        # Sort candidates: highest |pct_change| first
        candidates.sort(key=lambda m: abs(m.pct_change), reverse=True)

        hysteresis = s.laabh_quant_intraday_scanner_hysteresis_pct
        max_replacements = s.laabh_quant_intraday_scanner_max_replacements

        pairs: list[ReplacementPair] = []
        used_evict: set[str] = set()
        used_admit: set[str] = set()

        for evict_sym, evict_momentum in eviction_pool:
            if len(pairs) >= max_replacements:
                break
            if evict_sym in used_evict:
                continue
            for candidate in candidates:
                if candidate.symbol in used_admit:
                    continue
                admit_momentum = abs(candidate.pct_change)
                if admit_momentum >= evict_momentum + hysteresis:
                    pairs.append(ReplacementPair(
                        evict_symbol=evict_sym,
                        admit_instrument={
                            "id": candidate.id,
                            "symbol": candidate.symbol,
                            "name": candidate.name,
                        },
                        admit_momentum_pct=admit_momentum,
                        evict_momentum_pct=evict_momentum,
                    ))
                    used_evict.add(evict_sym)
                    used_admit.add(candidate.symbol)
                    break

        if pairs:
            logger.info(
                f"[SCANNER] {len(pairs)} replacement(s): "
                + ", ".join(
                    f"{p.evict_symbol}({p.evict_momentum_pct:.1f}%)"
                    f" → {p.admit_instrument['symbol']}({p.admit_momentum_pct:.1f}%)"
                    for p in pairs
                )
            )
        return pairs

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    async def _fetch_live_movers(
        self, trading_date: date, ban_set: set[str]
    ) -> list[LiveMover]:
        """Pull prev_close + latest intraday close for all F&O instruments.

        Returns instruments where intraday data is available for today.
        """
        async with session_scope() as session:
            # Prev close: most recent price_daily row before trading_date
            d_window_start = trading_date - timedelta(days=14)
            daily_q = (
                select(
                    Instrument.id,
                    Instrument.symbol,
                    Instrument.company_name.label("name"),
                    PriceDaily.date,
                    PriceDaily.close,
                    PriceDaily.volume,
                )
                .join(PriceDaily, PriceDaily.instrument_id == Instrument.id)
                .where(
                    and_(
                        Instrument.is_fno.is_(True),
                        Instrument.is_active.is_(True),
                        PriceDaily.date < trading_date,
                        PriceDaily.date >= d_window_start,
                    )
                )
                .order_by(Instrument.id, PriceDaily.date.desc())
            )
            daily_rows = (await session.execute(daily_q)).all()

            # Build prev_close + 5d avg volume per instrument
            prev_close_map: dict[uuid.UUID, tuple[str, str, float, int]] = {}
            vol_accumulator: dict[uuid.UUID, list[int]] = {}
            for row in daily_rows:
                if row.symbol in ban_set:
                    continue
                if row.id not in prev_close_map and row.close is not None:
                    prev_close_map[row.id] = (row.symbol, row.name, float(row.close), 0)
                vol_accumulator.setdefault(row.id, [])
                if len(vol_accumulator[row.id]) < 5 and row.volume is not None:
                    vol_accumulator[row.id].append(int(row.volume))

            if not prev_close_map:
                return []

            # Latest intraday bar for today (most recent close per instrument).
            # Single self-join query: the subquery finds each instrument's max
            # timestamp, the outer join fetches the close at that exact bar.
            # This avoids both the cross-product risk of timestamp.in_() and the
            # asyncpg dialect issues with SQLAlchemy's tuple_().in_().
            session_open_ist = _IST.localize(
                datetime.combine(trading_date, time(9, 15))
            )
            session_close_ist = _IST.localize(
                datetime.combine(trading_date, time(15, 30))
            )
            latest_subq = (
                select(
                    PriceIntraday.instrument_id,
                    func.max(PriceIntraday.timestamp).label("latest_ts"),
                )
                .where(
                    and_(
                        PriceIntraday.instrument_id.in_(list(prev_close_map.keys())),
                        PriceIntraday.timestamp >= session_open_ist,
                        PriceIntraday.timestamp <= session_close_ist,
                    )
                )
                .group_by(PriceIntraday.instrument_id)
                .subquery()
            )
            # Sentinel: if the subquery finds nothing, skip early before the join.
            sentinel = await session.execute(
                select(func.count()).select_from(latest_subq)
            )
            if sentinel.scalar() == 0:
                return []
            close_q = (
                select(
                    PriceIntraday.instrument_id,
                    PriceIntraday.close,
                )
                .join(
                    latest_subq,
                    and_(
                        PriceIntraday.instrument_id == latest_subq.c.instrument_id,
                        PriceIntraday.timestamp == latest_subq.c.latest_ts,
                    ),
                )
            )
            close_rows = (await session.execute(close_q)).all()
            current_close_map: dict[uuid.UUID, float] = {
                r.instrument_id: float(r.close) for r in close_rows
            }

        # Compute pct_change and build movers list
        settings = get_settings()
        movers: list[LiveMover] = []
        for inst_id, (symbol, name, prev_close, _) in prev_close_map.items():
            current = current_close_map.get(inst_id)
            if current is None or prev_close == 0:
                continue
            vols = vol_accumulator.get(inst_id, [])
            avg_vol = int(sum(vols) / len(vols)) if vols else 0
            if avg_vol < settings.fno_phase1_min_avg_volume_5d:
                continue
            pct = (current - prev_close) / prev_close * 100.0
            movers.append(LiveMover(
                id=inst_id,
                symbol=symbol,
                name=name,
                prev_close=prev_close,
                current_price=current,
                pct_change=pct,
                avg_volume_5d=avg_vol,
            ))

        movers.sort(key=lambda m: abs(m.pct_change), reverse=True)
        logger.debug(
            f"[SCANNER] {len(movers)} live movers computed for {trading_date}"
        )
        return movers

    @staticmethod
    async def _load_ban_set(trading_date: date) -> set[str]:
        async with session_scope() as session:
            q = select(FNOBanList.symbol).where(
                and_(
                    FNOBanList.ban_date == trading_date,
                    FNOBanList.is_active.is_(True),
                )
            )
            rows = (await session.execute(q)).all()
        return {r[0] for r in rows}
