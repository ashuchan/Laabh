"""Tests for quant EOD report formatter."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.quant.reports import _build_message, _holding_minutes


class _FakeTrade:
    def __init__(self, arm_id: str, pnl: float, costs: float = 100.0, mins: float = 15.0):
        self.arm_id = arm_id
        self.realized_pnl = Decimal(str(pnl))
        self.estimated_costs = Decimal(str(costs))
        now = datetime(2026, 5, 7, 6, 0, tzinfo=timezone.utc)
        from datetime import timedelta
        self.entry_at = now
        self.exit_at = now + timedelta(minutes=mins)
        self.status = "closed"
        self.portfolio_id = uuid.uuid4()


class _FakeDayState:
    starting_nav = Decimal("1000000")
    final_nav = Decimal("1032400")
    lockin_fired_at = None
    kill_switch_fired_at = None


def test_holding_minutes():
    t = _FakeTrade("A_orb", 100.0, mins=18.0)
    assert _holding_minutes(t) == pytest.approx(18.0)


def test_holding_minutes_no_exit():
    t = _FakeTrade("A_orb", 100.0)
    t.exit_at = None
    assert _holding_minutes(t) == 0.0


@pytest.mark.asyncio
async def test_build_message_renders():
    """Message renders without errors given mocked trades."""
    portfolio_id = uuid.uuid4()
    trading_date = date(2026, 5, 7)

    trades = [
        _FakeTrade("RELIANCE_orb", 8200, 200),
        _FakeTrade("NIFTY_momentum", 6800, 150),
        _FakeTrade("HDFCBANK_orb", 4100, 100),
        _FakeTrade("ICICIBANK_vol_breakout", -3200, 100),
    ]
    day_state = _FakeDayState()

    async def _fake_get(cls, pk):
        return day_state

    async def _fake_exec(q):
        class _R:
            def scalars(self):
                class _S:
                    def all(self):
                        return trades
                return _S()
        return _R()

    fake_session = MagicMock()
    fake_session.get = AsyncMock(return_value=day_state)
    fake_session.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=trades)))))
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)

    with patch("src.quant.reports.session_scope", return_value=fake_session):
        msg = await _build_message(portfolio_id, trading_date)

    assert "[QUANT]" in msg
    assert "NAV" in msg
    assert "Trades" in msg
    assert len(msg) <= 4096
    assert "RELIANCE_orb" in msg
