"""Historical-data feature store for the backtest harness.

Returns the same ``FeatureBundle`` shape as the live ``src.quant.feature_store``;
only the I/O layer underneath changes.

Sources:
  * Underlying OHLCV + volume + VWAP + realized vol + BB width:
        ``price_intraday`` (Task 1 schema, populated by Task 2 Dhan loader).
  * Options chain (ATM IV, OI, bid, ask):
        ``options_chain`` for end-of-day OI / IV (populated by Task 3 NSE
        bhavcopy loader); intraday premiums + bid/ask synthesized by
        ``chain_synthesizer.synthesize_chain`` (Task 5).
  * VIX value + regime: ``vix_ticks`` (populated by Task 4 VIX loader).
  * Risk-free rate: ``rbi_repo_history`` (populated by Task 4 RBI loader).
  * OFI raw inputs: returns 0 (the OFI primitive is excluded from backtest
    per spec §2.2 — L1 quote-size deltas aren't in retail historical data).

Strict no-lookahead invariant: every DB query filters
``WHERE timestamp <= virtual_time`` (or strictly less for the chain's prev-day
slope estimator, which uses *yesterday's* close). Task 13 wraps this store
with a runtime guard that asserts the invariant on every call.

Latency budget: < 50 ms per call (spec §8 Task 8 acceptance). Three small
parameterised queries per call dominate; async DB makes this trivial.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytz
from loguru import logger
from sqlalchemy import and_, select

from src.db import session_scope
from src.config import get_settings
from src.fno.chain_parser import ChainSnapshot
from src.models.fno_chain import OptionsChain
from src.models.fno_vix import VIXTick
from src.models.instrument import Instrument
from src.models.price_intraday import PriceIntraday
from src.models.rbi_repo_history import RBIRepoHistory
from src.quant.backtest.chain_synthesizer import (
    SynthesisInputs,
    estimate_smile_slope,
    synthesize_chain,
)
from src.quant.feature_store import FeatureBundle


_IST = pytz.timezone("Asia/Kolkata")
# Spread we layer on synthesized chain mid-prices to produce bid/ask.
# Real ATM spreads on liquid Indian options are 0.1–0.5%; use 0.3% midpoint.
_SYNTH_SPREAD_PCT = 0.003
# Annualization factor for 1-minute bar log-returns.
# 252 trading days × 375 min/day = 94,500 bars/yr (NSE intraday minutes).
_BARS_PER_YEAR_1MIN = 94_500


@dataclass
class _SmileCacheEntry:
    """Per (underlying, day) smile slope estimate, computed once at day start."""

    slope: float
    atm_iv: float


class BacktestFeatureStore:
    """Historical feature lookups returning the same shape as live mode.

    Construct one per backtest day. The store caches per-(underlying, day)
    smile slope and ATM IV from the prior day's chain so per-tick reads can
    skip those queries.
    """

    def __init__(
        self,
        *,
        trading_date: date,
        risk_free_rate: float | None = None,
        smile_method: str | None = None,
    ) -> None:
        self._trading_date = trading_date
        # Per-(underlying_id, date) prior-day chain → smile slope cache.
        self._smile_cache: dict[uuid.UUID, _SmileCacheEntry] = {}
        # Per-(underlying_id, virtual_time) synthesized chain cache. Bounded
        # to last 32 entries to keep memory under control over a full day.
        self._chain_cache: dict[tuple[uuid.UUID, datetime], list] = {}
        self._chain_cache_order: list[tuple[uuid.UUID, datetime]] = []
        self._risk_free_rate = risk_free_rate
        s = get_settings()
        self._smile_method = smile_method or s.laabh_quant_backtest_iv_smile_method

    # ------------------------------------------------------------------
    # Public API — drop-in replacement for live ``feature_store.get``.
    # ------------------------------------------------------------------

    async def get(
        self,
        underlying_id: uuid.UUID,
        virtual_time: datetime,
    ) -> FeatureBundle | None:
        """Return a FeatureBundle for ``underlying_id`` at ``virtual_time``.

        Returns None when no underlying bar exists at-or-before
        ``virtual_time`` for that underlying (matches live behavior under
        stale-data conditions).
        """
        async with session_scope() as session:
            # 1. Most recent bar at-or-before virtual_time
            current = await self._fetch_current_bar(session, underlying_id, virtual_time)
            if current is None:
                return None

            instrument = await session.get(Instrument, underlying_id)
            if instrument is None:
                return None

            # 2. History (last 30 min of 1-min bars before virtual_time)
            history = await self._fetch_history(
                session, underlying_id, virtual_time, minutes=30
            )

            # 3. Session-start bar + ORB high/low
            session_start_ltp, orb_high, orb_low = await self._fetch_orb(
                session, underlying_id, virtual_time
            )

            # 4. VIX (most recent at-or-before virtual_time)
            vix_value, vix_regime = await self._fetch_vix(session, virtual_time)

            # 5. Today's session-cumulative bars (for VWAP)
            session_open = self._session_open(virtual_time)
            session_bars = await self._fetch_history_since(
                session, underlying_id, session_open, virtual_time
            )

            # 6. Risk-free rate (RBI repo most-recent at-or-before today)
            r = await self._risk_free(session)

            # 7. Smile slope from prior day's chain (cached per underlying)
            slope, atm_iv_morning = await self._smile_for(session, underlying_id)

            # 8. Synthesize today's chain at virtual_time and pull ATM
            chain_rows = await self._synthesize_today_chain(
                session,
                instrument_id=underlying_id,
                spot=float(current.close),
                virtual_time=virtual_time,
                atm_iv=atm_iv_morning,
                slope=slope,
                r=r,
            )
            atm_iv, atm_oi, atm_bid, atm_ask = self._pick_atm(
                chain_rows, spot=float(current.close)
            )

        # 9. Compute derived stats (VWAP, vol, BB) — all pure functions
        underlying_ltp = float(current.close)
        underlying_volume_3min = sum(int(b.volume) for b in history[-3:])

        vwap_today = self._vwap_session(session_bars)
        rv_3 = self._realized_vol([float(b.close) for b in history[-3:]])
        rv_30 = self._realized_vol([float(b.close) for b in history])
        bb_width = self._bb_width([float(b.close) for b in history[-20:]])

        return FeatureBundle(
            underlying_id=underlying_id,
            underlying_symbol=instrument.symbol,
            captured_at=current.timestamp,
            underlying_ltp=underlying_ltp,
            underlying_volume_3min=float(underlying_volume_3min),
            vwap_today=vwap_today,
            realized_vol_3min=rv_3,
            realized_vol_30min=rv_30,
            atm_iv=atm_iv,
            atm_oi=atm_oi,
            atm_bid=atm_bid,
            atm_ask=atm_ask,
            # OFI primitive disabled in backtest per spec §2.2 — return 0
            # raw inputs (primitive will yield no signal).
            bid_volume_3min_change=0.0,
            ask_volume_3min_change=0.0,
            bb_width=bb_width,
            vix_value=vix_value,
            vix_regime=vix_regime,
            constituent_basket_value=None,
            session_start_ltp=session_start_ltp,
            orb_high=orb_high,
            orb_low=orb_low,
        )

    # ------------------------------------------------------------------
    # DB queries — strict ``timestamp <= virtual_time`` everywhere
    # ------------------------------------------------------------------

    @staticmethod
    async def _fetch_current_bar(
        session, underlying_id: uuid.UUID, virtual_time: datetime
    ) -> Any | None:
        q = (
            select(PriceIntraday)
            .where(
                and_(
                    PriceIntraday.instrument_id == underlying_id,
                    PriceIntraday.timestamp <= virtual_time,
                )
            )
            .order_by(PriceIntraday.timestamp.desc())
            .limit(1)
        )
        return (await session.execute(q)).scalar_one_or_none()

    @staticmethod
    async def _fetch_history(
        session,
        underlying_id: uuid.UUID,
        virtual_time: datetime,
        *,
        minutes: int,
    ) -> list:
        cutoff = virtual_time - timedelta(minutes=minutes)
        q = (
            select(PriceIntraday)
            .where(
                and_(
                    PriceIntraday.instrument_id == underlying_id,
                    PriceIntraday.timestamp >= cutoff,
                    PriceIntraday.timestamp <= virtual_time,
                )
            )
            .order_by(PriceIntraday.timestamp.asc())
        )
        return list((await session.execute(q)).scalars())

    @staticmethod
    async def _fetch_history_since(
        session,
        underlying_id: uuid.UUID,
        since: datetime,
        until: datetime,
    ) -> list:
        q = (
            select(PriceIntraday)
            .where(
                and_(
                    PriceIntraday.instrument_id == underlying_id,
                    PriceIntraday.timestamp >= since,
                    PriceIntraday.timestamp <= until,
                )
            )
            .order_by(PriceIntraday.timestamp.asc())
        )
        return list((await session.execute(q)).scalars())

    @staticmethod
    async def _fetch_orb(
        session, underlying_id: uuid.UUID, virtual_time: datetime
    ) -> tuple[float | None, float | None, float | None]:
        """Return (session_start_ltp, orb_high, orb_low) over first 30 min.

        Only the part of ORB that has *occurred* by virtual_time is read —
        early in the day the values may be partial. That's fine; primitives
        treat None as "warmup not complete".
        """
        session_open = BacktestFeatureStore._session_open(virtual_time)
        orb_end = session_open + timedelta(minutes=30)
        cap = min(virtual_time, orb_end)
        q = (
            select(PriceIntraday)
            .where(
                and_(
                    PriceIntraday.instrument_id == underlying_id,
                    PriceIntraday.timestamp >= session_open,
                    PriceIntraday.timestamp <= cap,
                )
            )
            .order_by(PriceIntraday.timestamp.asc())
        )
        rows = list((await session.execute(q)).scalars())
        if not rows:
            return None, None, None
        opens = [float(r.open) for r in rows]
        highs = [float(r.high) for r in rows]
        lows = [float(r.low) for r in rows]
        return opens[0], max(highs), min(lows)

    @staticmethod
    async def _fetch_vix(session, virtual_time: datetime) -> tuple[float, str]:
        q = (
            select(VIXTick)
            .where(VIXTick.timestamp <= virtual_time)
            .order_by(VIXTick.timestamp.desc())
            .limit(1)
        )
        row = (await session.execute(q)).scalar_one_or_none()
        if row is None:
            # Sensible default; primitives that key off VIX will treat as "normal"
            return 15.0, "neutral"
        return float(row.vix_value), str(row.regime)

    async def _risk_free(self, session) -> float:
        """Return decimal repo rate — uses RBI history, falls back to override or 6.5%."""
        if self._risk_free_rate is not None:
            return float(self._risk_free_rate)
        q = (
            select(RBIRepoHistory.repo_rate_pct)
            .where(RBIRepoHistory.date <= self._trading_date)
            .order_by(RBIRepoHistory.date.desc())
            .limit(1)
        )
        row = (await session.execute(q)).first()
        if row is None:
            return 0.065
        # repo_rate_pct is stored as percent (e.g. 6.5000 for 6.5%)
        return float(row[0]) / 100.0

    async def _smile_for(
        self, session, underlying_id: uuid.UUID
    ) -> tuple[float, float]:
        """Return (smile_slope, atm_iv) for ``underlying_id`` based on prior chain.

        Cached per-underlying so we hit the chain table at most once per day
        per underlying, not once per tick.
        """
        cached = self._smile_cache.get(underlying_id)
        if cached is not None:
            return cached.slope, cached.atm_iv

        # Pull the prior trading day's full chain row set for this underlying.
        # We scan back up to 7 calendar days to land on the most recent
        # day with bhavcopy data.
        cutoff_lo = self._trading_date - timedelta(days=7)
        cutoff_hi = self._trading_date  # strictly less than today
        q = (
            select(OptionsChain)
            .where(
                and_(
                    OptionsChain.instrument_id == underlying_id,
                    OptionsChain.snapshot_at < self._localize_morning(),
                    OptionsChain.snapshot_at
                    >= datetime.combine(cutoff_lo, datetime.min.time()).replace(
                        tzinfo=timezone.utc
                    ),
                )
            )
            .order_by(OptionsChain.snapshot_at.desc())
        )
        rows = list((await session.execute(q)).scalars())
        if not rows:
            self._smile_cache[underlying_id] = _SmileCacheEntry(slope=0.0, atm_iv=0.20)
            return 0.0, 0.20
        # Take rows from the single most-recent snapshot date
        latest_date = rows[0].snapshot_at.date()
        latest_rows = [r for r in rows if r.snapshot_at.date() == latest_date]
        # Build a ChainSnapshot just for slope estimation
        from src.fno.chain_parser import ChainRow as _ChainRow
        snap_rows = [
            _ChainRow(
                instrument_id=r.instrument_id,
                expiry_date=r.expiry_date,
                strike_price=r.strike_price,
                option_type=r.option_type,
                ltp=r.ltp,
                iv=float(r.iv) if r.iv is not None else None,
                underlying_ltp=r.underlying_ltp,
            )
            for r in latest_rows
        ]
        chain = ChainSnapshot(
            instrument_id=underlying_id,
            snapshot_at=latest_rows[0].snapshot_at,
            rows=snap_rows,
            underlying_ltp=latest_rows[0].underlying_ltp,
        )
        slope = estimate_smile_slope(chain)
        # ATM IV: closest strike to underlying_ltp
        atm_iv = self._atm_iv_from_snapshot(chain)
        self._smile_cache[underlying_id] = _SmileCacheEntry(slope=slope, atm_iv=atm_iv)
        return slope, atm_iv

    @staticmethod
    def _atm_iv_from_snapshot(chain: ChainSnapshot) -> float:
        """Pick the ATM strike's IV from a ChainSnapshot; default 0.20."""
        if chain.underlying_ltp is None or not chain.rows:
            return 0.20
        spot = float(chain.underlying_ltp)
        # Prefer CE; fall back to PE
        candidates = [r for r in chain.rows if r.iv is not None]
        if not candidates:
            return 0.20
        atm = min(candidates, key=lambda r: abs(float(r.strike_price) - spot))
        iv = atm.iv if atm.iv is not None else 0.20
        return max(0.01, float(iv))

    # ------------------------------------------------------------------
    # Chain synthesis (cached per virtual_time)
    # ------------------------------------------------------------------

    async def _synthesize_today_chain(
        self,
        session,
        *,
        instrument_id: uuid.UUID,
        spot: float,
        virtual_time: datetime,
        atm_iv: float,
        slope: float,
        r: float,
    ) -> list:
        cache_key = (instrument_id, virtual_time)
        cached = self._chain_cache.get(cache_key)
        if cached is not None:
            return cached

        # Strikes: ±15 around the ATM, in steps that match the underlying's
        # tick. For a synthetic chain we use percent-based steps so the same
        # logic works for indices and stocks.
        step_pct = 0.005  # 0.5% spacing
        strikes = [spot * (1 + step_pct * i) for i in range(-15, 16)]
        # Round to a sensible tick (₹0.5 or ₹50 depending on price scale).
        tick = 50.0 if spot > 1000 else 0.5
        strikes = sorted({round(k / tick) * tick for k in strikes if k > 0})

        # Find the nearest expiry strictly after today using the latest
        # snapshot's expiries.
        expiry = await self._next_expiry(session, instrument_id)
        if expiry is None:
            return []

        rows = synthesize_chain(
            SynthesisInputs(
                instrument_id=instrument_id,
                underlying_ltp=spot,
                strikes=strikes,
                expiry_date=expiry,
                as_of=virtual_time,
                atm_iv=atm_iv,
                repo_rate=r,
                smile_method=self._smile_method,  # type: ignore[arg-type]
                smile_slope=slope,
            )
        )

        self._chain_cache[cache_key] = rows
        self._chain_cache_order.append(cache_key)
        if len(self._chain_cache_order) > 32:
            old_key = self._chain_cache_order.pop(0)
            self._chain_cache.pop(old_key, None)
        return rows

    async def _next_expiry(self, session, instrument_id: uuid.UUID) -> date | None:
        """Return the nearest expiry > trading_date from options_chain history."""
        q = (
            select(OptionsChain.expiry_date)
            .where(
                and_(
                    OptionsChain.instrument_id == instrument_id,
                    OptionsChain.expiry_date >= self._trading_date,
                )
            )
            .order_by(OptionsChain.expiry_date.asc())
            .limit(1)
        )
        row = (await session.execute(q)).first()
        if row is None or row[0] is None:
            return None
        return row[0]

    @staticmethod
    def _pick_atm(
        chain_rows: list, *, spot: float
    ) -> tuple[float, float, Decimal, Decimal]:
        """Pick the ATM CE+PE rows, return (iv, oi, bid, ask).

        ``oi`` is 0 because synthesized chains don't carry OI; consumers
        that need OI can read it from the Tier 2 close-of-day chain
        elsewhere. We synthesize bid/ask by applying ±_SYNTH_SPREAD_PCT
        to the BS premium midpoint.
        """
        if not chain_rows:
            return 0.20, 0.0, Decimal("0"), Decimal("0")
        ce_rows = [r for r in chain_rows if r.option_type == "CE"]
        if not ce_rows:
            return 0.20, 0.0, Decimal("0"), Decimal("0")
        atm = min(ce_rows, key=lambda r: abs(float(r.strike_price) - spot))
        iv = float(atm.iv) if atm.iv is not None else 0.20
        ltp = float(atm.ltp or 0)
        half = ltp * (_SYNTH_SPREAD_PCT / 2.0)
        bid = Decimal(str(round(max(0.0, ltp - half), 2)))
        ask = Decimal(str(round(ltp + half, 2)))
        return iv, 0.0, bid, ask

    # ------------------------------------------------------------------
    # Pure-function helpers (mirror the live store's math)
    # ------------------------------------------------------------------

    @staticmethod
    def _vwap_session(bars: list) -> float:
        """Cumulative VWAP across all session bars."""
        total_v = sum(int(b.volume) for b in bars)
        if not bars or total_v == 0:
            return float(bars[-1].close) if bars else 0.0
        # Prefer per-bar VWAP when present, fall back to typical price (HLC/3).
        num = 0.0
        for b in bars:
            v = int(b.volume)
            if v == 0:
                continue
            px = (
                float(b.vwap)
                if b.vwap is not None
                else (float(b.high) + float(b.low) + float(b.close)) / 3.0
            )
            num += px * v
        return num / total_v if total_v > 0 else float(bars[-1].close)

    @staticmethod
    def _realized_vol(closes: list[float]) -> float:
        """Annualised realised σ from log-returns of ``closes``."""
        if len(closes) < 2:
            return 0.0
        rets = [
            math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes))
            if closes[i - 1] > 0 and closes[i] > 0
        ]
        if not rets:
            return 0.0
        n = len(rets)
        mean = sum(rets) / n
        var = sum((r - mean) ** 2 for r in rets) / max(n - 1, 1)
        return math.sqrt(var * _BARS_PER_YEAR_1MIN)

    @staticmethod
    def _bb_width(closes: list[float]) -> float:
        if len(closes) < 2:
            return 0.0
        n = len(closes)
        mean = sum(closes) / n
        std = math.sqrt(sum((p - mean) ** 2 for p in closes) / max(n - 1, 1))
        if mean == 0:
            return 0.0
        return (4 * std) / mean  # 4σ / mean = (upper - lower) / middle for 2σ bands

    # ------------------------------------------------------------------
    # Time helpers
    # ------------------------------------------------------------------

    def _localize_morning(self) -> datetime:
        """IST 09:00 of the trading date — used as cutoff for prior-day chain."""
        return _IST.localize(datetime.combine(self._trading_date, time(9, 0)))

    @staticmethod
    def _session_open(virtual_time: datetime) -> datetime:
        """Return 09:15 IST on the same calendar day as ``virtual_time``."""
        ist = (
            virtual_time.astimezone(_IST)
            if virtual_time.tzinfo is not None
            else _IST.localize(virtual_time)
        )
        return _IST.localize(datetime.combine(ist.date(), time(9, 15)))
