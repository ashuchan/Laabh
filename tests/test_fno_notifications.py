"""Tests for F&O notification formatters."""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.fno.notifications import (
    _escape,
    format_daily_summary,
    format_entry_alert,
    format_hard_exit_alert,
    format_signal_alert,
    format_stop_alert,
    format_target_alert,
)


def test_escape_dots_and_parens() -> None:
    result = _escape("1.5% (gain)")
    assert "\\." in result
    assert "\\(" in result
    assert "\\)" in result


def test_escape_plus_sign() -> None:
    result = _escape("+100")
    assert "\\+" in result


def test_escape_no_modification_for_clean_text() -> None:
    result = _escape("NIFTY")
    assert result == "NIFTY"


def test_format_signal_alert_contains_symbol() -> None:
    msg = format_signal_alert(
        symbol="NIFTY",
        direction="bullish",
        thesis="Strong momentum from FII buying.",
        confidence=0.75,
        composite_score=7.5,
        strategy_name="long_call",
        iv_regime="low",
        iv_rank=25.0,
    )
    assert "NIFTY" in msg
    assert "BULLISH" in msg or "bullish" in msg.lower()
    assert "Long Call" in msg or "long" in msg.lower()
    assert "7" in msg  # composite score


def test_format_signal_alert_bullish_emoji() -> None:
    msg = format_signal_alert("TCS", "bullish", ".", 0.6, 6.0, "long_call", "neutral", 40.0)
    assert "🟢" in msg


def test_format_signal_alert_bearish_emoji() -> None:
    msg = format_signal_alert("TCS", "bearish", ".", 0.6, 4.0, "long_put", "neutral", 60.0)
    assert "🔴" in msg


def test_format_signal_alert_iv_rank_none() -> None:
    msg = format_signal_alert("TCS", "bullish", ".", 0.6, 6.0, "long_call", "low", None)
    assert "N/A" in msg


def test_format_entry_alert_contains_fill_price() -> None:
    msg = format_entry_alert(
        symbol="RELIANCE",
        strategy_name="bull_call_spread",
        fill_price=Decimal("102.50"),
        strike=Decimal("2900"),
        option_type="CE",
        lots=2,
        stop_price=Decimal("50"),
        target_price=Decimal("200"),
    )
    assert "RELIANCE" in msg
    assert "102" in msg
    assert "2900" in msg
    assert "50" in msg
    assert "200" in msg


def test_format_stop_alert_contains_pnl() -> None:
    msg = format_stop_alert("INFY", Decimal("45"), Decimal("100"), Decimal("-2750"))
    assert "INFY" in msg
    assert "45" in msg
    assert "🛑" in msg


def test_format_target_alert_contains_profit() -> None:
    msg = format_target_alert("HDFC", Decimal("210"), Decimal("100"), Decimal("5500"))
    assert "HDFC" in msg
    assert "🎯" in msg
    assert "5500" in msg


def test_format_hard_exit_contains_time_mention() -> None:
    msg = format_hard_exit_alert("WIPRO", Decimal("95"), Decimal("100"), Decimal("-250"))
    assert "WIPRO" in msg
    assert "14:30" in msg
    assert "⏰" in msg


def test_format_daily_summary_contains_all_phases() -> None:
    msg = format_daily_summary(
        run_date="2026-04-27",
        phase1_passed=45,
        phase2_passed=12,
        phase3_proceed=4,
        trades_entered=3,
        net_pnl=Decimal("4200"),
    )
    assert "45" in msg
    assert "12" in msg
    assert "4" in msg
    assert "4200" in msg
    assert "📊" in msg


def test_format_daily_summary_negative_pnl() -> None:
    msg = format_daily_summary("2026-04-27", 30, 8, 2, 2, Decimal("-1500"))
    assert "1500" in msg
