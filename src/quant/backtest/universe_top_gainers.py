"""Deterministic universe selection for backtest mode.

Replaces the LLM-driven Phase 1-3 candidate pipeline with a fully
reproducible top-movers selector running on the prior trading day's data.
This is the only thing in the backtest harness that does *not* mirror live
behavior — universe selection is decoupled because LLM output is not
historically reproducible.

Algorithm (spec §3.1, paraphrased):

  1. Pull all instruments where ``is_fno = true``.
  2. Drop instruments banned on the trading date (``fno_ban_list``).
  3. Compute prev_day_return for each instrument: (close[D-1] - close[D-2]) / close[D-2].
  4. Filter: avg_volume_5d > FNO_PHASE1_MIN_AVG_VOLUME_5D.
  5. Filter: close[D-1] > LAABH_QUANT_BACKTEST_MIN_PRICE (default ₹50).
  6. Rank candidates:
        - Top N by prev_day_return (gainers)
        - Top M by absolute prev_day_return (movers — captures big losers)
        - Top P by overnight gap (open[D] / close[D-1] - 1) — when intraday data available
  7. Deduplicate, take top ``backtest_universe_size`` unique.

Decision Note (data source):
  * Steps 3 and 4 read from ``price_daily`` (already populated for every F&O
    underlying — required by other consumers like `fno.market_movers`).
  * Step 6's gap calculation needs intraday open from ``price_intraday``
    (Task 1 schema). When that data isn't loaded for the date, we silently
    drop the gap-bucket and fill from gainers/movers.
  * Ban list filter uses ``is_active = true`` and matches the live filter
    in ``src.fno.ban_list``.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytz
from loguru import logger
from sqlalchemy import and_, func, select

from src.config import get_settings
from src.db import session_scope
from src.models.fno_ban import FNOBanList
from src.models.instrument import Instrument
from src.models.price import PriceDaily
from src.models.price_intraday import PriceIntraday
from src.quant.universe import UniverseSelector


_IST = pytz.timezone("Asia/Kolkata")


class TopGainersUniverseSelector(UniverseSelector):
    """Backtest-mode universe = top movers from the prior trading day.

    Constructed once per backtest run; ``select(date)`` is called per
    trading day. Holds no per-day state — safe to share across days, and
    safe to use across worker processes (joblib parallelism in the perf
    patch).
    """

    def __init__(
        self,
        *,
        gainers_count: int | None = None,
        movers_count: int | None = None,
        gappers_count: int | None = None,
        min_price: float | None = None,
        min_avg_volume_5d: int | None = None,
        size_cap: int | None = None,
    ) -> None:
        s = get_settings()
        self._gainers = (
            gainers_count
            if gainers_count is not None
            else s.laabh_quant_backtest_top_gainers_count
        )
        self._movers = (
            movers_count
            if movers_count is not None
            else s.laabh_quant_backtest_top_movers_count
        )
        self._gappers = (
            gappers_count
            if gappers_count is not None
            else s.laabh_quant_backtest_top_gappers_count
        )
        self._min_price = (
            min_price if min_price is not None else s.laabh_quant_backtest_min_price
        )
        self._min_avg_volume_5d = (
            min_avg_volume_5d
            if min_avg_volume_5d is not None
            else s.fno_phase1_min_avg_volume_5d
        )
        self._size_cap = (
            size_cap
            if size_cap is not None
            else s.laabh_quant_backtest_universe_size
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def select(self, trading_date: date) -> list[dict]:
        """Return ordered universe for ``trading_date`` (deterministic).

        Output dicts have ``id``, ``symbol``, and ``name`` — same shape as
        ``LLMUniverseSelector`` for orchestrator-side compatibility.
        """
        async with session_scope() as session:
            ban_set = await self._load_ban_set(session, trading_date)
            candidates = await self._load_candidates(
                session, trading_date, ban_set=ban_set
            )

        if not candidates:
            logger.warning(
                f"TopGainersUniverseSelector: no candidates found for {trading_date} "
                f"— is price_daily populated for the prior 5 days?"
            )
            return []

        # Three rank dimensions, deduplicated.
        gainers = sorted(
            candidates, key=lambda c: c["prev_day_return"], reverse=True
        )[: self._gainers]
        movers = sorted(
            candidates, key=lambda c: abs(c["prev_day_return"]), reverse=True
        )[: self._movers]
        gappers = sorted(
            (c for c in candidates if c["overnight_gap"] is not None),
            key=lambda c: abs(c["overnight_gap"]),  # type: ignore[arg-type]
            reverse=True,
        )[: self._gappers]

        seen: set[str] = set()
        out: list[dict] = []
        for bucket in (gainers, movers, gappers):
            for c in bucket:
                if c["symbol"] in seen:
                    continue
                seen.add(c["symbol"])
                out.append(
                    {
                        "id": c["id"],
                        "symbol": c["symbol"],
                        "name": c["name"],
                    }
                )
                if len(out) >= self._size_cap:
                    break
            if len(out) >= self._size_cap:
                break

        logger.info(
            f"TopGainersUniverseSelector: selected {len(out)} underlyings for "
            f"{trading_date} (banned {len(ban_set)}, candidates {len(candidates)})"
        )
        return out

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _load_ban_set(session, trading_date: date) -> set[str]:
        """Return symbols banned (active) on ``trading_date``."""
        q = select(FNOBanList.symbol).where(
            and_(
                FNOBanList.ban_date == trading_date,
                FNOBanList.is_active.is_(True),
            )
        )
        rows = (await session.execute(q)).all()
        return {r[0] for r in rows}

    async def _load_candidates(
        self,
        session,
        trading_date: date,
        *,
        ban_set: set[str],
    ) -> list[dict[str, Any]]:
        """Build per-instrument candidate rows with returns and volume.

        Returns dicts with ``id``, ``symbol``, ``name``, ``prev_day_return``,
        ``avg_volume_5d``, ``prev_close``, ``overnight_gap``.
        """
        # We need close on (D-1) and (D-2). The clearest way is two ranked
        # subqueries; for portability we just pull a 7-day window of daily
        # rows for every F&O instrument and aggregate in Python. Row count
        # is ~200 instruments × 7 days = 1400 rows — trivial.
        d_minus_7 = trading_date - timedelta(days=14)  # 14 cal-days for 5 trading days
        q = (
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
                    PriceDaily.date >= d_minus_7,
                )
            )
            .order_by(Instrument.id, PriceDaily.date)
        )
        rows = (await session.execute(q)).all()

        # Group by instrument
        by_inst: dict[Any, list[dict]] = {}
        for r in rows:
            if r.symbol in ban_set:
                continue
            by_inst.setdefault(r.id, []).append(
                {
                    "id": r.id,
                    "symbol": r.symbol,
                    "name": r.name,
                    "date": r.date,
                    "close": float(r.close) if r.close is not None else None,
                    "volume": int(r.volume) if r.volume is not None else 0,
                }
            )

        out: list[dict] = []
        for inst_id, daily_rows in by_inst.items():
            # Most recent first
            daily_rows.sort(key=lambda d: d["date"], reverse=True)
            if len(daily_rows) < 2:
                continue  # Not enough history to compute prev-day return
            d1, d2 = daily_rows[0], daily_rows[1]
            if d1["close"] is None or d2["close"] is None or d2["close"] == 0:
                continue
            prev_day_return = (d1["close"] - d2["close"]) / d2["close"]

            # Avg volume across up to 5 most recent prior days
            recent5 = daily_rows[:5]
            avg_volume = (
                sum(x["volume"] for x in recent5) / len(recent5)
                if recent5
                else 0
            )

            # Apply filters
            if avg_volume <= self._min_avg_volume_5d:
                continue
            if d1["close"] <= self._min_price:
                continue

            overnight_gap = await self._fetch_overnight_gap(
                session,
                instrument_id=inst_id,
                trading_date=trading_date,
                prev_close=d1["close"],
            )

            out.append(
                {
                    "id": inst_id,
                    "symbol": d1["symbol"],
                    "name": d1["name"],
                    "prev_day_return": prev_day_return,
                    "avg_volume_5d": avg_volume,
                    "prev_close": d1["close"],
                    "overnight_gap": overnight_gap,
                }
            )
        return out

    @staticmethod
    async def _fetch_overnight_gap(
        session,
        *,
        instrument_id: Any,
        trading_date: date,
        prev_close: float,
    ) -> float | None:
        """Return ``open[D] / prev_close - 1`` if intraday data is available, else None."""
        # First-bar of the day in IST → UTC: 09:15 IST = 03:45 UTC
        session_open_ist = _IST.localize(datetime.combine(trading_date, time(9, 15)))
        # Window of 5 minutes around session open to catch the first bar
        session_open_end = session_open_ist + timedelta(minutes=5)
        q = (
            select(PriceIntraday.open)
            .where(
                and_(
                    PriceIntraday.instrument_id == instrument_id,
                    PriceIntraday.timestamp >= session_open_ist,
                    PriceIntraday.timestamp < session_open_end,
                )
            )
            .order_by(PriceIntraday.timestamp.asc())
            .limit(1)
        )
        row = (await session.execute(q)).first()
        if row is None or row[0] is None or prev_close == 0:
            return None
        return float(row[0]) / prev_close - 1.0
