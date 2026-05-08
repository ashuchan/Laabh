"""Tests for the EQUITY_TRADING_ENABLED master switch.

Covers every gating point the flag controls:
  - Settings default preserves current behaviour.
  - Engine chokepoint refuses equity, allows F&O, no-ops when flag enabled.
  - OrderBook.check_pending_orders adds an instrument join when flag off.
  - Reconciler skips equity DAILY_CRITICAL entries when flag off.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytz

from src.config import Settings
from src.trading.engine import EquityTradingDisabled, _refuse_if_equity_disabled


@asynccontextmanager
async def _scope_yielding(session):
    yield session


# ---------------------------------------------------------------------------
# Settings default
# ---------------------------------------------------------------------------

def test_flag_field_default_is_true():
    """Default = True so adding the flag is a no-op for existing deployments."""
    field = Settings.model_fields["equity_trading_enabled"]
    assert field.default is True


# ---------------------------------------------------------------------------
# Engine chokepoint: _refuse_if_equity_disabled
# ---------------------------------------------------------------------------

async def test_refuse_no_db_hit_when_flag_enabled(monkeypatch):
    """When the flag is on (default), the chokepoint must not even open a session."""
    settings = MagicMock(equity_trading_enabled=True)
    monkeypatch.setattr("src.trading.engine.get_settings", lambda: settings)

    def _boom(*a, **kw):
        raise AssertionError("session_scope must not be opened when flag=True")

    monkeypatch.setattr("src.trading.engine.session_scope", _boom)

    await _refuse_if_equity_disabled(uuid.uuid4(), trade_type="BUY", quantity=10)


async def test_refuse_raises_for_equity_when_flag_disabled(monkeypatch):
    settings = MagicMock(equity_trading_enabled=False)
    monkeypatch.setattr("src.trading.engine.get_settings", lambda: settings)

    instr = MagicMock(is_fno=False, symbol="RELIANCE")
    session = MagicMock()
    session.get = AsyncMock(return_value=instr)
    monkeypatch.setattr(
        "src.trading.engine.session_scope", lambda: _scope_yielding(session)
    )

    with pytest.raises(EquityTradingDisabled, match="RELIANCE"):
        await _refuse_if_equity_disabled(
            uuid.uuid4(), trade_type="BUY", quantity=10
        )


async def test_refuse_allows_fno_when_flag_disabled(monkeypatch):
    """F&O instruments must pass through even when the equity flag is off."""
    settings = MagicMock(equity_trading_enabled=False)
    monkeypatch.setattr("src.trading.engine.get_settings", lambda: settings)

    instr = MagicMock(is_fno=True, symbol="NIFTY25MAY24000CE")
    session = MagicMock()
    session.get = AsyncMock(return_value=instr)
    monkeypatch.setattr(
        "src.trading.engine.session_scope", lambda: _scope_yielding(session)
    )

    await _refuse_if_equity_disabled(
        uuid.uuid4(), trade_type="BUY", quantity=50
    )


async def test_refuse_silent_when_instrument_missing(monkeypatch):
    """A missing instrument is downstream's problem — don't shadow it as a flag refusal."""
    settings = MagicMock(equity_trading_enabled=False)
    monkeypatch.setattr("src.trading.engine.get_settings", lambda: settings)

    session = MagicMock()
    session.get = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "src.trading.engine.session_scope", lambda: _scope_yielding(session)
    )

    await _refuse_if_equity_disabled(
        uuid.uuid4(), trade_type="BUY", quantity=10
    )


# ---------------------------------------------------------------------------
# OrderBook.check_pending_orders SQL filtering
# ---------------------------------------------------------------------------

class _FakeScalars:
    def all(self):
        return []


class _FakeQueryResult:
    def scalars(self):
        return _FakeScalars()


class _CapturingSession:
    """Records the most recent ``execute`` statement for assertion."""

    def __init__(self):
        self.sql: str | None = None

    async def execute(self, stmt):
        self.sql = str(stmt.compile(compile_kwargs={"literal_binds": False}))
        return _FakeQueryResult()


async def test_check_pending_orders_joins_instrument_when_flag_disabled(monkeypatch):
    """With the flag off, the query must filter on ``instruments.is_fno=true``."""
    from src.trading.order_book import OrderBook

    settings = MagicMock(equity_trading_enabled=False)
    monkeypatch.setattr("src.trading.order_book.get_settings", lambda: settings)

    session = _CapturingSession()
    monkeypatch.setattr(
        "src.trading.order_book.session_scope", lambda: _scope_yielding(session)
    )

    n = await OrderBook().check_pending_orders()
    assert n == 0
    assert session.sql is not None
    sql_lower = session.sql.lower()
    assert "instruments" in sql_lower
    assert "is_fno" in sql_lower


async def test_check_pending_orders_no_join_when_flag_enabled(monkeypatch):
    """With the flag on, the query stays a plain pending-orders select (no Instrument join)."""
    from src.trading.order_book import OrderBook

    settings = MagicMock(equity_trading_enabled=True)
    monkeypatch.setattr("src.trading.order_book.get_settings", lambda: settings)

    session = _CapturingSession()
    monkeypatch.setattr(
        "src.trading.order_book.session_scope", lambda: _scope_yielding(session)
    )

    n = await OrderBook().check_pending_orders()
    assert n == 0
    assert session.sql is not None
    assert "is_fno" not in session.sql.lower()


# ---------------------------------------------------------------------------
# Reconciler: equity catch-up entries skipped when flag off
# ---------------------------------------------------------------------------

def _now_in_ist():
    return pytz.timezone("Asia/Kolkata").localize(datetime(2026, 5, 8, 16, 0))


async def _setup_reconciler_mocks(monkeypatch, *, equity_enabled: bool):
    settings = MagicMock()
    settings.equity_trading_enabled = equity_enabled
    settings.timezone = "Asia/Kolkata"
    monkeypatch.setattr("src.scheduler_reconciler.get_settings", lambda: settings)

    # Force every DAILY_CRITICAL job into the grace window so the flag is
    # the only thing that decides which job_ids reach _last_success_at.
    def _within_window(now_local, hour, minute):
        return now_local - timedelta(minutes=10)
    monkeypatch.setattr(
        "src.scheduler_reconciler._last_expected_firing", _within_window
    )

    seen: list[str] = []

    async def fake_last_success(name):
        seen.append(name)
        return None

    monkeypatch.setattr(
        "src.scheduler_reconciler._last_success_at", fake_last_success
    )
    # Returning None makes reconcile_missed skip scheduling for every job
    # while still recording the _last_success_at lookup, which is what we
    # use to assert which job_ids were considered.
    monkeypatch.setattr(
        "src.scheduler_reconciler._resolve_job_func", lambda sched, jid: None
    )
    return seen


async def test_reconciler_skips_equity_when_flag_disabled(monkeypatch):
    from src.scheduler_reconciler import reconcile_missed

    seen = await _setup_reconciler_mocks(monkeypatch, equity_enabled=False)
    await reconcile_missed(MagicMock(), as_of=_now_in_ist())

    assert "equity_morning_allocation" not in seen
    assert "equity_eod_squareoff" not in seen
    # Non-equity daily-critical jobs must still be considered.
    assert "daily_snapshot" in seen


async def test_reconciler_includes_equity_when_flag_enabled(monkeypatch):
    from src.scheduler_reconciler import reconcile_missed

    seen = await _setup_reconciler_mocks(monkeypatch, equity_enabled=True)
    await reconcile_missed(MagicMock(), as_of=_now_in_ist())

    assert "equity_morning_allocation" in seen
    assert "equity_eod_squareoff" in seen
