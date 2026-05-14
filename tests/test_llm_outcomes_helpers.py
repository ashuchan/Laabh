"""Pure-function coverage for src.fno.llm_outcomes.

Targets the helpers introduced for Phase 0.3 outcome attribution and the
v10 counterfactual P&L pricer (review fixes P1 #3 and the earlier P1 #3
function-level implementation). Async DB paths require Postgres and live
elsewhere; this file is sync-only and runs without a DB.
"""
from __future__ import annotations

from datetime import date

from src.fno.llm_outcomes import (
    _calendar_days_for_trading,
    _legs_for_structure,
    _trading_days_between,
)


# ---------------------------------------------------------------------------
# _trading_days_between (already in production for >1 phase but uncovered)
# ---------------------------------------------------------------------------


def test_trading_days_zero_when_same_day() -> None:
    assert _trading_days_between(date(2026, 5, 14), date(2026, 5, 14)) == 0


def test_trading_days_skips_weekends() -> None:
    # Thursday → Monday = 2 trading days (Sat+Sun skipped).
    assert _trading_days_between(date(2026, 5, 14), date(2026, 5, 18)) == 2


def test_trading_days_full_week() -> None:
    # Mon → following Mon = 5 weekdays.
    assert _trading_days_between(date(2026, 5, 11), date(2026, 5, 18)) == 5


def test_trading_days_negative_span_returns_zero() -> None:
    assert _trading_days_between(date(2026, 5, 20), date(2026, 5, 10)) == 0


def test_trading_days_two_weeks() -> None:
    assert _trading_days_between(date(2026, 5, 4), date(2026, 5, 18)) == 10


# ---------------------------------------------------------------------------
# _calendar_days_for_trading
# ---------------------------------------------------------------------------


def test_calendar_days_for_trading_5_business_days_is_7_calendar() -> None:
    assert _calendar_days_for_trading(5) == 7


def test_calendar_days_for_trading_zero() -> None:
    assert _calendar_days_for_trading(0) == 0


# ---------------------------------------------------------------------------
# _legs_for_structure — every supported structure must round-trip through
# the lookup with the right (option_type, strike, sign) triples.
# ---------------------------------------------------------------------------


def test_legs_bull_call_spread_debit() -> None:
    """+CE(low), -CE(high) — net debit, profits as underlying rises."""
    legs = _legs_for_structure("bull_call_spread", [19500, 19700], 0.4)
    assert legs == [("CE", 19500.0, 1), ("CE", 19700.0, -1)]


def test_legs_bear_put_spread_debit() -> None:
    """+PE(high), -PE(low) — net debit, profits as underlying falls."""
    legs = _legs_for_structure("bear_put_spread", [19500, 19700], -0.4)
    assert legs == [("PE", 19700.0, 1), ("PE", 19500.0, -1)]


def test_legs_bull_put_spread_credit() -> None:
    """-PE(high), +PE(low) — net credit, profits if underlying holds above."""
    legs = _legs_for_structure("bull_put_spread", [19500, 19700], 0.3)
    assert legs == [("PE", 19700.0, -1), ("PE", 19500.0, 1)]


def test_legs_bear_call_spread_credit() -> None:
    """-CE(low), +CE(high) — net credit, profits if underlying holds below."""
    legs = _legs_for_structure("bear_call_spread", [19500, 19700], -0.3)
    assert legs == [("CE", 19500.0, -1), ("CE", 19700.0, 1)]


def test_legs_long_call_single_strike() -> None:
    assert _legs_for_structure("long_call", [19500], 0.7) == [("CE", 19500.0, 1)]


def test_legs_long_put_single_strike() -> None:
    assert _legs_for_structure("long_put", [19500], -0.7) == [("PE", 19500.0, 1)]


def test_legs_long_straddle_atm() -> None:
    """+CE @ ATM, +PE @ same strike — neutral volatility-long."""
    legs = _legs_for_structure("long_straddle", [19500], 0.0)
    assert legs == [("CE", 19500.0, 1), ("PE", 19500.0, 1)]


def test_legs_short_strangle_wings() -> None:
    """-CE(high), -PE(low) — credit, profits if underlying stays in range."""
    legs = _legs_for_structure("short_strangle", [19500, 19700], 0.0)
    assert legs == [("CE", 19700.0, -1), ("PE", 19500.0, -1)]


def test_legs_iron_condor_four_strikes() -> None:
    """Order: [put_long, put_short, call_short, call_long] — sorted ascending."""
    legs = _legs_for_structure("iron_condor", [19000, 19300, 19700, 20000], 0.0)
    assert legs == [
        ("PE", 19000.0, 1),
        ("PE", 19300.0, -1),
        ("CE", 19700.0, -1),
        ("CE", 20000.0, 1),
    ]


def test_legs_iron_condor_unsorted_input_still_sorted() -> None:
    """Strikes can arrive out of order from the LLM — function must sort."""
    legs = _legs_for_structure("iron_condor", [20000, 19000, 19700, 19300], 0.0)
    assert legs == [
        ("PE", 19000.0, 1),
        ("PE", 19300.0, -1),
        ("CE", 19700.0, -1),
        ("CE", 20000.0, 1),
    ]


def test_legs_unknown_structure_single_strike_falls_back_to_conviction_sign() -> None:
    """Unknown structure + 1 strike + positive conviction → long CE."""
    assert _legs_for_structure("mystery", [19500], 0.5) == [("CE", 19500.0, 1)]
    assert _legs_for_structure("mystery", [19500], -0.5) == [("PE", 19500.0, 1)]


def test_legs_unknown_structure_multi_strike_returns_none() -> None:
    """Unknown structure + >1 strikes is unresolvable; caller marks unobservable."""
    assert _legs_for_structure("mystery", [19500, 19700], 0.5) is None


def test_legs_unknown_structure_single_strike_no_conviction() -> None:
    """Without a conviction sign we can't infer direction → None."""
    assert _legs_for_structure("mystery", [19500], None) is None
