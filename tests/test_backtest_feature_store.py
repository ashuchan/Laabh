"""Unit tests for ``BacktestFeatureStore``.

Pure-function helpers (VWAP, realized vol, BB width, ATM picking, smile-from-
snapshot) are tested directly. The full ``get()`` path is tested with a
mocked async session that returns canned rows per query. No DB required.
"""
from __future__ import annotations

import math
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytz

from src.fno.chain_parser import ChainRow, ChainSnapshot
from src.quant.backtest.feature_store import (
    _BARS_PER_YEAR_1MIN,
    _SYNTH_SPREAD_PCT,
    BacktestFeatureStore,
)


_IST = pytz.timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Pure-function helpers
# ---------------------------------------------------------------------------

def _bar(close, volume, *, vwap=None, high=None, low=None, ts=None):
    return SimpleNamespace(
        close=Decimal(str(close)),
        volume=int(volume),
        vwap=Decimal(str(vwap)) if vwap is not None else None,
        high=Decimal(str(high if high is not None else close)),
        low=Decimal(str(low if low is not None else close)),
        open=Decimal(str(close)),
        timestamp=ts or _IST.localize(datetime(2026, 4, 27, 9, 15)),
    )


def test_vwap_session_uses_per_bar_vwap_when_present():
    bars = [_bar(100, 10, vwap=99), _bar(101, 20, vwap=100)]
    # vwap = (99*10 + 100*20) / 30 = 99.667
    assert BacktestFeatureStore._vwap_session(bars) == pytest.approx(99.6667, abs=1e-3)


def test_vwap_session_falls_back_to_typical_price():
    """When per-bar vwap is missing, use (H+L+C)/3."""
    bars = [_bar(100, 10, high=102, low=98, vwap=None)]
    # Typical = (102+98+100)/3 = 100
    assert BacktestFeatureStore._vwap_session(bars) == pytest.approx(100.0, abs=1e-6)


def test_vwap_session_zero_volume_falls_back_to_last_close():
    bars = [_bar(100, 0), _bar(102, 0)]
    assert BacktestFeatureStore._vwap_session(bars) == pytest.approx(102.0)


def test_vwap_session_empty_returns_zero():
    assert BacktestFeatureStore._vwap_session([]) == 0.0


def test_realized_vol_flat_series_zero():
    assert BacktestFeatureStore._realized_vol([100.0, 100.0, 100.0]) == pytest.approx(0.0)


def test_realized_vol_two_bars_zero_sample_var():
    # Single log-return → sample variance is 0
    assert BacktestFeatureStore._realized_vol([100.0, 101.0]) == pytest.approx(0.0)


def test_realized_vol_positive_for_real_movement():
    closes = [100.0, 101.0, 99.5, 100.2, 102.0]
    rv = BacktestFeatureStore._realized_vol(closes)
    assert rv > 0
    # Sanity: with 1-min bars, a 1% absolute swing implies highly annualised σ
    assert rv < 100.0  # not infinite


def test_realized_vol_uses_correct_annualization_factor():
    """Annualisation factor must be ~94,500 (252 trading days × 375 min/day)."""
    assert _BARS_PER_YEAR_1MIN == 94_500


def test_bb_width_constant_series_zero():
    assert BacktestFeatureStore._bb_width([100.0] * 20) == pytest.approx(0.0)


def test_bb_width_positive_for_movement():
    assert BacktestFeatureStore._bb_width([100.0 + i for i in range(20)]) > 0.0


def test_bb_width_single_value_zero():
    assert BacktestFeatureStore._bb_width([100.0]) == 0.0


# ---------------------------------------------------------------------------
# Session open helper
# ---------------------------------------------------------------------------

def test_session_open_aware_input():
    vt = _IST.localize(datetime(2026, 4, 27, 11, 0))
    so = BacktestFeatureStore._session_open(vt)
    assert so == _IST.localize(datetime(2026, 4, 27, 9, 15))


def test_session_open_utc_input():
    vt = datetime(2026, 4, 27, 5, 30, tzinfo=timezone.utc)  # 11:00 IST
    so = BacktestFeatureStore._session_open(vt)
    assert so == _IST.localize(datetime(2026, 4, 27, 9, 15))


# ---------------------------------------------------------------------------
# Smile-from-snapshot
# ---------------------------------------------------------------------------

def test_atm_iv_from_snapshot_picks_closest_strike():
    rows = [
        ChainRow(instrument_id=uuid.uuid4(), expiry_date=date(2026, 5, 28),
                 strike_price=Decimal("19000"), option_type="CE", iv=0.25),
        ChainRow(instrument_id=uuid.uuid4(), expiry_date=date(2026, 5, 28),
                 strike_price=Decimal("20000"), option_type="CE", iv=0.18),  # ATM
        ChainRow(instrument_id=uuid.uuid4(), expiry_date=date(2026, 5, 28),
                 strike_price=Decimal("21000"), option_type="CE", iv=0.22),
    ]
    chain = ChainSnapshot(
        instrument_id=uuid.uuid4(),
        snapshot_at=datetime(2026, 5, 7),
        rows=rows,
        underlying_ltp=Decimal("20050"),
    )
    assert BacktestFeatureStore._atm_iv_from_snapshot(chain) == pytest.approx(0.18)


def test_atm_iv_from_snapshot_default_for_empty():
    chain = ChainSnapshot(
        instrument_id=uuid.uuid4(),
        snapshot_at=datetime(2026, 5, 7),
        rows=[],
        underlying_ltp=Decimal("20000"),
    )
    assert BacktestFeatureStore._atm_iv_from_snapshot(chain) == 0.20


def test_atm_iv_from_snapshot_default_for_no_underlying():
    chain = ChainSnapshot(
        instrument_id=uuid.uuid4(),
        snapshot_at=datetime(2026, 5, 7),
        rows=[ChainRow(instrument_id=uuid.uuid4(), expiry_date=date(2026, 5, 28),
                       strike_price=Decimal("20000"), option_type="CE", iv=0.18)],
        underlying_ltp=None,
    )
    assert BacktestFeatureStore._atm_iv_from_snapshot(chain) == 0.20


# ---------------------------------------------------------------------------
# _pick_atm — synthetic chain → (iv, oi, bid, ask)
# ---------------------------------------------------------------------------

def test_pick_atm_synthetic_bid_ask_have_correct_spread():
    rows = [
        ChainRow(instrument_id=uuid.uuid4(), expiry_date=date(2026, 5, 28),
                 strike_price=Decimal("20000"), option_type="CE",
                 iv=0.18, ltp=Decimal("100.00")),
    ]
    iv, oi, bid, ask = BacktestFeatureStore._pick_atm(rows, spot=20000.0)
    assert iv == pytest.approx(0.18)
    assert oi == 0.0
    spread = float(ask) - float(bid)
    # Spread is _SYNTH_SPREAD_PCT × ltp, centered on ltp
    assert spread == pytest.approx(100.0 * _SYNTH_SPREAD_PCT, abs=0.05)


def test_pick_atm_no_ce_rows_returns_default():
    rows = [
        ChainRow(instrument_id=uuid.uuid4(), expiry_date=date(2026, 5, 28),
                 strike_price=Decimal("20000"), option_type="PE",
                 iv=0.18, ltp=Decimal("50.0")),
    ]
    iv, oi, bid, ask = BacktestFeatureStore._pick_atm(rows, spot=20000.0)
    assert iv == 0.20
    assert oi == 0.0
    assert bid == Decimal("0")
    assert ask == Decimal("0")


def test_pick_atm_empty_rows_returns_default():
    iv, oi, bid, ask = BacktestFeatureStore._pick_atm([], spot=20000.0)
    assert iv == 0.20 and oi == 0.0


def test_pick_atm_picks_closest_to_spot():
    rows = [
        ChainRow(instrument_id=uuid.uuid4(), expiry_date=date(2026, 5, 28),
                 strike_price=Decimal("19500"), option_type="CE",
                 iv=0.20, ltp=Decimal("550.0")),
        ChainRow(instrument_id=uuid.uuid4(), expiry_date=date(2026, 5, 28),
                 strike_price=Decimal("20100"), option_type="CE",
                 iv=0.18, ltp=Decimal("110.0")),  # closest to 20050
        ChainRow(instrument_id=uuid.uuid4(), expiry_date=date(2026, 5, 28),
                 strike_price=Decimal("20500"), option_type="CE",
                 iv=0.22, ltp=Decimal("60.0")),
    ]
    iv, _, bid, ask = BacktestFeatureStore._pick_atm(rows, spot=20050.0)
    assert iv == pytest.approx(0.18)
    # bid/ask centered on 110
    mid = (float(bid) + float(ask)) / 2.0
    assert mid == pytest.approx(110.0, abs=0.05)


# ---------------------------------------------------------------------------
# End-to-end smoke (mocked session)
# ---------------------------------------------------------------------------

class _FakeSession:
    """Returns canned rows per `select` based on the model being queried.

    We pattern-match the SQL by inspecting the FROM clause's table name, which
    is enough to route each of the seven queries the store issues per get().
    """

    def __init__(self, *, instrument, current_bar, history, vix, prior_chain_rows,
                 next_expiry, repo_pct):
        self._instrument = instrument
        self._current_bar = current_bar
        self._history = history
        self._vix = vix
        self._prior_chain_rows = prior_chain_rows
        self._next_expiry = next_expiry
        self._repo_pct = repo_pct
        self.calls = 0

    async def get(self, model, key):
        return self._instrument

    async def execute(self, stmt):
        self.calls += 1
        s = str(stmt).lower()
        if "price_intraday" in s:
            # First query: most recent bar (LIMIT 1, ORDER BY DESC).
            # Distinguish by .order_by direction.
            if "desc" in s and "limit" in s and "1" in s:
                return SimpleNamespace(
                    scalar_one_or_none=lambda: self._current_bar,
                    scalars=lambda: SimpleNamespace(
                        __iter__=lambda self_: iter([self._current_bar])
                    ),
                )
            # Otherwise it's history (ASC, no limit) — return list
            return SimpleNamespace(
                scalars=lambda: iter(self._history),
                first=lambda: (self._history[0],) if self._history else None,
            )
        if "vix_ticks" in s:
            return SimpleNamespace(scalar_one_or_none=lambda: self._vix)
        if "rbi_repo_history" in s:
            return SimpleNamespace(first=lambda: (self._repo_pct,))
        if "options_chain" in s:
            # _smile_for queries OptionsChain ORDER BY snapshot_at DESC
            # _next_expiry queries OptionsChain.expiry_date ASC LIMIT 1
            if "expiry_date" in s and "limit" in s:
                return SimpleNamespace(first=lambda: (self._next_expiry,))
            return SimpleNamespace(scalars=lambda: iter(self._prior_chain_rows))
        return SimpleNamespace(scalar_one_or_none=lambda: None, first=lambda: None)


@pytest.mark.asyncio
async def test_get_returns_none_when_no_underlying_bar(monkeypatch):
    """No bar at-or-before virtual_time → return None."""
    store = BacktestFeatureStore(trading_date=date(2026, 4, 27))

    class _Empty:
        async def get(self, *_): return None
        async def execute(self, _stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    @asynccontextmanager
    async def _scope():
        yield _Empty()

    monkeypatch.setattr("src.quant.backtest.feature_store.session_scope", _scope)

    result = await store.get(uuid.uuid4(), _IST.localize(datetime(2026, 4, 27, 11, 0)))
    assert result is None


@pytest.mark.asyncio
async def test_get_returns_bundle_with_correct_shape(monkeypatch):
    """Smoke test: end-to-end ``get`` returns a FeatureBundle with the live shape."""
    store = BacktestFeatureStore(trading_date=date(2026, 4, 27), risk_free_rate=0.065)
    inst_id = uuid.uuid4()
    instrument = SimpleNamespace(id=inst_id, symbol="RELIANCE")

    # 30 minutes of 1-min bars, prices walking from 2500 to 2510
    base_ts = _IST.localize(datetime(2026, 4, 27, 10, 30))
    history = [
        _bar(2500 + i * 0.3, 1000, ts=base_ts + timedelta(minutes=i))
        for i in range(30)
    ]
    current_bar = history[-1]

    vix = SimpleNamespace(vix_value=Decimal("16.5"), regime="neutral")

    prior_chain_rows = [
        SimpleNamespace(
            instrument_id=inst_id,
            expiry_date=date(2026, 5, 28),
            strike_price=Decimal("2500"),
            option_type="CE",
            iv=Decimal("0.20"),
            ltp=Decimal("50"),
            underlying_ltp=Decimal("2500"),
            snapshot_at=_IST.localize(datetime(2026, 4, 26, 15, 30)),
        ),
        SimpleNamespace(
            instrument_id=inst_id,
            expiry_date=date(2026, 5, 28),
            strike_price=Decimal("2550"),
            option_type="CE",
            iv=Decimal("0.18"),
            ltp=Decimal("25"),
            underlying_ltp=Decimal("2500"),
            snapshot_at=_IST.localize(datetime(2026, 4, 26, 15, 30)),
        ),
    ]

    fake = _FakeSession(
        instrument=instrument,
        current_bar=current_bar,
        history=history,
        vix=vix,
        prior_chain_rows=prior_chain_rows,
        next_expiry=date(2026, 5, 28),
        repo_pct=Decimal("6.5000"),
    )

    @asynccontextmanager
    async def _scope():
        yield fake

    monkeypatch.setattr("src.quant.backtest.feature_store.session_scope", _scope)

    bundle = await store.get(inst_id, base_ts + timedelta(minutes=29))
    assert bundle is not None
    assert bundle.underlying_id == inst_id
    assert bundle.underlying_symbol == "RELIANCE"
    assert bundle.captured_at == current_bar.timestamp
    # OFI raw inputs are zero in backtest per spec §2.2
    assert bundle.bid_volume_3min_change == 0.0
    assert bundle.ask_volume_3min_change == 0.0
    # VIX comes through as float + regime str
    assert bundle.vix_value == 16.5
    assert bundle.vix_regime == "neutral"
    # ATM IV is positive
    assert bundle.atm_iv > 0.0
    assert bundle.atm_bid >= Decimal("0")
    assert bundle.atm_ask >= bundle.atm_bid


# ---------------------------------------------------------------------------
# Caching / construction
# ---------------------------------------------------------------------------

def test_construct_uses_settings_for_smile_method():
    store = BacktestFeatureStore(trading_date=date(2026, 4, 27))
    assert store._smile_method in ("flat", "linear", "sabr")


def test_construct_overrides_smile_method():
    store = BacktestFeatureStore(trading_date=date(2026, 4, 27), smile_method="flat")
    assert store._smile_method == "flat"


def test_smile_cache_starts_empty():
    store = BacktestFeatureStore(trading_date=date(2026, 4, 27))
    assert store._smile_cache == {}
    assert store._chain_cache == {}
