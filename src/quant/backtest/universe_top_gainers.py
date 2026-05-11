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

from collections import defaultdict
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
        sector_heat_enabled: bool | None = None,
        sector_heat_threshold_pct: float | None = None,
        sector_heat_count: int | None = None,
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
        self._sector_heat_enabled = (
            sector_heat_enabled
            if sector_heat_enabled is not None
            else s.laabh_quant_backtest_sector_heat_enabled
        )
        self._sector_heat_threshold_pct = (
            sector_heat_threshold_pct
            if sector_heat_threshold_pct is not None
            else s.laabh_quant_backtest_sector_heat_threshold_pct
        )
        self._sector_heat_count = (
            sector_heat_count
            if sector_heat_count is not None
            else s.laabh_quant_backtest_sector_heat_count
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

        # Four rank dimensions, deduplicated: gainers, movers, gappers, sector heat.
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
        sector_heat: list[dict] = []
        if self._sector_heat_enabled:
            sector_heat = self._build_sector_heat_bucket(
                candidates,
                threshold_pct=self._sector_heat_threshold_pct,
                per_sector_count=self._sector_heat_count,
            )

        seen: set[str] = set()
        out: list[dict] = []
        for bucket in (gainers, movers, gappers, sector_heat):
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
            f"{trading_date} (banned {len(ban_set)}, candidates {len(candidates)}, "
            f"sector_heat_raw={len(sector_heat)} pre-dedup)"
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
                Instrument.sector,
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
                    "sector": r.sector,
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
                    "sector": d1.get("sector"),
                    "prev_day_return": prev_day_return,
                    "avg_volume_5d": avg_volume,
                    "prev_close": d1["close"],
                    "overnight_gap": overnight_gap,
                }
            )
        return out

    @staticmethod
    def _build_sector_heat_bucket(
        candidates: list[dict[str, Any]],
        *,
        threshold_pct: float,
        per_sector_count: int,
    ) -> list[dict]:
        """Add top N liquid names from any sector whose avg D-1 return >= threshold.

        A sector is "hot" when its F&O members collectively moved >= threshold_pct
        on the prior day — suggesting a rotation or macro catalyst that will likely
        persist into D. We add the top movers from hot sectors regardless of their
        individual rank in the gainers/movers buckets.
        """
        sector_returns: dict[str, list[float]] = defaultdict(list)
        sector_candidates: dict[str, list[dict]] = defaultdict(list)
        for c in candidates:
            sector = c.get("sector")
            if not sector:
                continue
            sector_returns[sector].append(c["prev_day_return"] * 100.0)
            sector_candidates[sector].append(c)

        out: list[dict] = []
        for sector, returns in sector_returns.items():
            avg_return = sum(returns) / len(returns)
            if abs(avg_return) < threshold_pct:
                continue
            # Top movers within this hot sector
            top_in_sector = sorted(
                sector_candidates[sector],
                key=lambda c: abs(c["prev_day_return"]),
                reverse=True,
            )[:per_sector_count]
            out.extend(top_in_sector)

        return out

    @staticmethod
    async def _fetch_overnight_gap(
        session,
        *,
        instrument_id: Any,
        trading_date: date,
        prev_close: float,
    ) -> float | None:
        """Return ``open[D] / prev_close - 1`` if intraday data is available, else None.

        The window is 10 minutes (previously 5 min) because the 3-min bar that
        spans 09:15–09:18 IST is written at bar-close time (~09:18) and may not
        be present when the selector is called at exactly 09:15. A 10-minute
        window reliably catches the first completed bar without straying into the
        second bar's open.
        """
        # First-bar of the day in IST → UTC: 09:15 IST = 03:45 UTC
        session_open_ist = _IST.localize(datetime.combine(trading_date, time(9, 15)))
        # Widened to 10 min to catch the first completed 3-min bar even when
        # universe selection runs slightly after 09:15.
        session_open_end = session_open_ist + timedelta(minutes=10)
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
