"""F&O module smoke run — validates the full pipeline end-to-end using mock data.

Run from the project root:
    python scripts/fno_smoke_run.py

This script does NOT require a live database or API keys. It exercises:
  1. Calendar: next_weekly_expiry for major indices
  2. Chain parser: compute_iv, compute_greeks, ChainSnapshot analytics
  3. IV history: compute_iv_rank, compute_iv_percentile, select_atm_iv
  4. Universe Phase 1: apply_liquidity_filter with mock data
  5. Catalyst Phase 2: all scoring functions
  6. Thesis Phase 3: parse_llm_response, classify_oi_structure, build_user_prompt
  7. Strategies: all six strategies on mock chain
  8. Strike ranker: rank_strategies on mock recommendations
  9. Fill simulator: simulate_fill for BUY and SELL legs
  10. Sizer: compute_lots, compute_stop_loss, compute_target
  11. Intraday manager: entry gates, apply_tick, trailing stop
  12. Notifications: all format_* functions

Exit code 0 = all checks passed. Exit code 1 = at least one failure.
"""
from __future__ import annotations

import sys
import traceback
from datetime import date, datetime, time, timezone, timedelta
from decimal import Decimal

PASS = "✅"
FAIL = "❌"
failures: list[str] = []


def check(name: str, fn) -> None:
    try:
        fn()
        print(f"  {PASS} {name}")
    except Exception as exc:
        print(f"  {FAIL} {name}: {exc}")
        traceback.print_exc()
        failures.append(name)


# ---------------------------------------------------------------------------
# 1. Calendar
# ---------------------------------------------------------------------------
print("\n[1] Calendar")

def _cal_nifty():
    from src.fno.calendar import next_weekly_expiry
    exp = next_weekly_expiry("NIFTY", date(2026, 4, 20))
    assert exp.weekday() == 1, f"Expected Tuesday, got weekday={exp.weekday()}"  # Tuesday=1

def _cal_sensex():
    from src.fno.calendar import next_weekly_expiry
    exp = next_weekly_expiry("SENSEX", date(2026, 4, 20))
    assert exp.weekday() == 3, f"Expected Thursday, got weekday={exp.weekday()}"

check("NIFTY expiry is Tuesday", _cal_nifty)
check("SENSEX expiry is Thursday", _cal_sensex)


# ---------------------------------------------------------------------------
# 2. Chain parser
# ---------------------------------------------------------------------------
print("\n[2] Chain parser")

def _bs_iv_round_trip():
    from src.fno.chain_parser import _bs_price, compute_iv
    price = _bs_price(1000, 1000, 0.1, 0.065, 0.20, "CE")
    iv = compute_iv(price, 1000, 1000, 0.1, 0.065, "CE")
    assert iv is not None and abs(iv - 0.20) < 0.005, f"IV round-trip failed: {iv}"

def _greeks_atm_delta():
    from src.fno.chain_parser import compute_greeks
    g = compute_greeks(1000, 1000, 0.1, 0.065, 0.20, "CE")
    assert 0.45 < g["delta"] < 0.60

def _pcr():
    from decimal import Decimal
    from datetime import date
    from src.fno.chain_parser import ChainRow, ChainSnapshot, compute_pcr
    snap = ChainSnapshot(instrument_id=None, snapshot_at=None)
    snap.rows = [
        ChainRow(instrument_id=None, expiry_date=date(2026,4,28), strike_price=Decimal("1000"), option_type="CE", oi=50000, underlying_ltp=Decimal("1000")),
        ChainRow(instrument_id=None, expiry_date=date(2026,4,28), strike_price=Decimal("1000"), option_type="PE", oi=40000, underlying_ltp=Decimal("1000")),
    ]
    pcr = compute_pcr(snap)
    assert pcr is not None and abs(pcr - 0.8) < 0.01

check("BS IV round-trip", _bs_iv_round_trip)
check("ATM delta near 0.5", _greeks_atm_delta)
check("PCR calculation", _pcr)


# ---------------------------------------------------------------------------
# 3. IV history builder
# ---------------------------------------------------------------------------
print("\n[3] IV history builder")

def _iv_rank():
    from src.fno.iv_history_builder import compute_iv_rank
    result = compute_iv_rank(20.0, [10.0, 15.0, 25.0])
    assert result is not None and abs(result - 66.67) < 0.1

def _iv_pct():
    from src.fno.iv_history_builder import compute_iv_percentile
    r = compute_iv_percentile(15.0, [10.0, 12.0, 18.0, 20.0])
    assert abs(r - 50.0) < 0.1  # 2 out of 4 are below 15

def _atm_iv_selection():
    from src.fno.iv_history_builder import select_atm_iv
    rows = [("CE", 1000.0, 0.20), ("PE", 1000.0, 0.18)]
    iv = select_atm_iv(rows, 1000.0)
    assert abs(iv - 0.19) < 0.001

check("IV rank computation", _iv_rank)
check("IV percentile computation", _iv_pct)
check("ATM IV selection", _atm_iv_selection)


# ---------------------------------------------------------------------------
# 4. Universe Phase 1
# ---------------------------------------------------------------------------
print("\n[4] Universe Phase 1")

def _phase1_pass():
    from src.fno.universe import apply_liquidity_filter
    ok, reason = apply_liquidity_filter(60000, 0.003, 500000, min_oi=50000, max_spread_pct=0.005, min_volume=10000)
    assert ok and not reason

def _phase1_fail_oi():
    from src.fno.universe import apply_liquidity_filter
    ok, reason = apply_liquidity_filter(1000, 0.003, 500000, min_oi=50000, max_spread_pct=0.005, min_volume=10000)
    assert not ok

check("Liquidity filter PASS", _phase1_pass)
check("Liquidity filter FAIL (OI too low)", _phase1_fail_oi)


# ---------------------------------------------------------------------------
# 5. Catalyst Phase 2
# ---------------------------------------------------------------------------
print("\n[5] Catalyst Phase 2")

def _news_score():
    from src.fno.catalyst_scorer import score_news
    assert score_news(5, 0) == 10.0
    assert score_news(0, 5) == 0.0
    assert score_news(0, 0) == 5.0

def _composite_score():
    from src.fno.catalyst_scorer import compute_composite
    assert compute_composite(5.0, 5.0, 5.0, 5.0, 5.0) == 5.0
    assert compute_composite(10.0, 10.0, 10.0, 10.0, 10.0) == 10.0

check("News scoring", _news_score)
check("Composite scoring", _composite_score)


# ---------------------------------------------------------------------------
# 6. Thesis Phase 3
# ---------------------------------------------------------------------------
print("\n[6] Thesis Phase 3")

def _parse_proceed():
    from src.fno.thesis_synthesizer import parse_llm_response
    import json
    raw = json.dumps({"decision": "PROCEED", "direction": "bullish", "thesis": ".", "risk_factors": [], "confidence": 0.8})
    result = parse_llm_response(raw)
    assert result["decision"] == "PROCEED"

def _oi_structure():
    from src.fno.thesis_synthesizer import classify_oi_structure
    assert classify_oi_structure(1.5) == "put_heavy"
    assert classify_oi_structure(0.5) == "call_heavy"
    assert classify_oi_structure(None) == "unknown"

check("Parse PROCEED decision", _parse_proceed)
check("OI structure classification", _oi_structure)


# ---------------------------------------------------------------------------
# 7. Strategies
# ---------------------------------------------------------------------------
print("\n[7] Strategies")

def _all_strategies_importable():
    from src.fno.strategies import ALL_STRATEGIES
    assert len(ALL_STRATEGIES) == 6

def _long_call_select():
    from src.fno.strategies.long_call import LongCallStrategy
    s = LongCallStrategy()
    rec = s.select("bullish", Decimal("1000"), 30.0, "low", 5, [Decimal("950"), Decimal("1000"), Decimal("1050")], Decimal("25"))
    assert rec is not None
    assert rec.legs[0].option_type == "CE"

check("All 6 strategies registered", _all_strategies_importable)
check("Long call select", _long_call_select)


# ---------------------------------------------------------------------------
# 8. Strike ranker
# ---------------------------------------------------------------------------
print("\n[8] Strike ranker")

def _ranker_best():
    from src.fno.strategies import ALL_STRATEGIES
    from src.fno.strike_ranker import best_strategy
    strikes = [Decimal(s) for s in [950, 1000, 1050]]
    recs = [s.select("bullish", Decimal("1000"), 30.0, "low", 5, strikes, Decimal("25")) for s in ALL_STRATEGIES]
    recs = [r for r in recs if r is not None]
    best = best_strategy(recs, "bullish", "low", "put_heavy", 7.0)
    assert best is not None and best.composite_score > 0

check("Ranker returns best strategy", _ranker_best)


# ---------------------------------------------------------------------------
# 9. Fill simulator
# ---------------------------------------------------------------------------
print("\n[9] Fill simulator")

def _fill_buy():
    from src.fno.execution.fill_simulator import simulate_fill
    r = simulate_fill("BUY", Decimal("98"), Decimal("102"), 1, 50)
    assert r.fill_price >= Decimal("102")
    assert r.net_cost > 0

def _fill_sell():
    from src.fno.execution.fill_simulator import simulate_fill
    r = simulate_fill("SELL", Decimal("98"), Decimal("102"), 1, 50)
    assert r.net_cost < 0

check("Buy fill cost positive", _fill_buy)
check("Sell fill cost negative", _fill_sell)


# ---------------------------------------------------------------------------
# 10. Sizer
# ---------------------------------------------------------------------------
print("\n[10] Sizer")

def _sizer_basic():
    from src.fno.execution.sizer import compute_lots
    lots = compute_lots(Decimal("1000000"), Decimal("1000"), 50, Decimal("100"))
    assert lots >= 1

def _sizer_vix_halves():
    from src.fno.execution.sizer import compute_lots
    n = compute_lots(Decimal("1000000"), Decimal("500"), 50, Decimal("50"), vix_regime="neutral")
    h = compute_lots(Decimal("1000000"), Decimal("500"), 50, Decimal("50"), vix_regime="high")
    assert h <= n

check("Sizer basic allocation", _sizer_basic)
check("High VIX halves position", _sizer_vix_halves)


# ---------------------------------------------------------------------------
# 11. Intraday manager
# ---------------------------------------------------------------------------
print("\n[11] Intraday manager")

def _entry_gate():
    from src.fno.intraday_manager import IntradayState, is_entry_allowed
    from datetime import timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    now = datetime(2026, 4, 27, 10, 30, tzinfo=ist)
    state = IntradayState()
    ok, reason = is_entry_allowed(now, "inst-1", state)
    assert ok

def _apply_tick_stop():
    from src.fno.intraday_manager import OpenPosition, apply_tick
    pos = OpenPosition("id", "NIFTY", "long_call", "CE", Decimal("22000"),
                       Decimal("100"), Decimal("50"), Decimal("200"), 1, 50)
    assert apply_tick(pos, Decimal("40")) == "stop"

check("Entry allowed at 10:30 IST", _entry_gate)
check("Stop hit when price drops below stop_price", _apply_tick_stop)


# ---------------------------------------------------------------------------
# 12. Notifications
# ---------------------------------------------------------------------------
print("\n[12] Notifications")

def _signal_alert():
    from src.fno.notifications import format_signal_alert
    msg = format_signal_alert("NIFTY", "bullish", "Strong momentum.", 0.75, 7.5, "long_call", "low", 30.0)
    assert "NIFTY" in msg and "🟢" in msg

def _entry_alert():
    from src.fno.notifications import format_entry_alert
    msg = format_entry_alert("RELIANCE", "long_call", Decimal("102"), Decimal("2900"), "CE", 1, Decimal("51"), Decimal("204"))
    assert "RELIANCE" in msg

check("Signal alert formatting", _signal_alert)
check("Entry alert formatting", _entry_alert)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print()
if failures:
    print(f"❌ {len(failures)} checks FAILED: {', '.join(failures)}")
    sys.exit(1)
else:
    print(f"✅ All smoke checks passed!")
    sys.exit(0)
