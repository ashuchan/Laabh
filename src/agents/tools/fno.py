"""F&O tool executors: get_options_chain, get_iv_context, enumerate_eligible_strategies,
get_strategy_payoff, check_ban_list.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from src.agents.tools.registry import ToolContext

# Eligible strategies per (direction, iv_regime, vix_regime)
_STRATEGY_TABLE: dict[tuple[str, str, str], list[str]] = {
    # (direction, iv_regime, vix_regime)
    ("bullish",  "cheap",   "low"):    ["long_call", "call_debit_spread", "bull_put_spread"],
    ("bullish",  "cheap",   "neutral"): ["long_call", "call_debit_spread"],
    ("bullish",  "cheap",   "high"):   ["call_debit_spread", "bull_put_spread"],
    ("bullish",  "fair",    "low"):    ["long_call", "call_debit_spread", "covered_call"],
    ("bullish",  "fair",    "neutral"): ["call_debit_spread", "bull_put_spread"],
    ("bullish",  "fair",    "high"):   ["bull_put_spread", "call_debit_spread"],
    ("bullish",  "rich",    "low"):    ["bull_put_spread"],
    ("bullish",  "rich",    "neutral"): ["bull_put_spread"],
    ("bullish",  "rich",    "high"):   ["bull_put_spread"],
    ("bearish",  "cheap",   "low"):    ["long_put", "put_debit_spread", "bear_call_spread"],
    ("bearish",  "cheap",   "neutral"): ["long_put", "put_debit_spread"],
    ("bearish",  "cheap",   "high"):   ["put_debit_spread", "bear_call_spread"],
    ("bearish",  "fair",    "low"):    ["long_put", "put_debit_spread"],
    ("bearish",  "fair",    "neutral"): ["put_debit_spread", "bear_call_spread"],
    ("bearish",  "fair",    "high"):   ["bear_call_spread"],
    ("bearish",  "rich",    "low"):    ["bear_call_spread"],
    ("bearish",  "rich",    "neutral"): ["bear_call_spread"],
    ("bearish",  "rich",    "high"):   ["bear_call_spread"],
    ("neutral",  "cheap",   "low"):    ["long_straddle", "long_strangle"],
    ("neutral",  "cheap",   "neutral"): ["long_straddle", "calendar_spread"],
    ("neutral",  "cheap",   "high"):   ["long_strangle"],
    ("neutral",  "fair",    "low"):    ["iron_condor", "iron_butterfly"],
    ("neutral",  "fair",    "neutral"): ["iron_condor"],
    ("neutral",  "fair",    "high"):   ["iron_condor", "put_debit_spread"],
    ("neutral",  "rich",    "low"):    ["iron_condor", "short_straddle"],
    ("neutral",  "rich",    "neutral"): ["iron_condor", "short_strangle"],
    ("neutral",  "rich",    "high"):   ["iron_condor"],
}


async def execute_get_options_chain(params: dict, ctx: "ToolContext") -> dict:
    """Fetch the current options chain snapshot for an F&O underlying."""
    underlying_id = params["underlying_id"]
    expiry_date = params["expiry_date"]
    snapshot_at = params.get("snapshot_at")

    if snapshot_at:
        time_filter = "AND oc.snapshot_at <= :snapshot_at"
        bind: dict = {
            "iid": str(underlying_id),
            "expiry": expiry_date,
            "snapshot_at": snapshot_at,
        }
    else:
        time_filter = ""
        bind = {"iid": str(underlying_id), "expiry": expiry_date}

    try:
        async with ctx.db() as db:
            result = await db.execute(
                text(f"""
                    SELECT strike_price, option_type, ltp, bid_price, ask_price,
                           bid_qty, ask_qty, volume, oi, oi_change, iv,
                           delta, gamma, theta, vega, underlying_ltp, snapshot_at
                    FROM options_chain oc
                    WHERE oc.instrument_id = :iid
                      AND oc.expiry_date = :expiry
                      {time_filter}
                    ORDER BY oc.snapshot_at DESC, oc.strike_price ASC
                    LIMIT 200
                """),
                bind,
            )
            rows = result.fetchall()
            return {
                "result": [
                    {
                        "strike": float(r[0]),
                        "type": r[1],
                        "ltp": float(r[2] or 0),
                        "bid": float(r[3] or 0),
                        "ask": float(r[4] or 0),
                        "bid_qty": r[5],
                        "ask_qty": r[6],
                        "volume": r[7],
                        "oi": r[8],
                        "oi_change": r[9],
                        "iv": float(r[10] or 0) if r[10] else None,
                        "delta": float(r[11] or 0) if r[11] else None,
                        "gamma": float(r[12] or 0) if r[12] else None,
                        "theta": float(r[13] or 0) if r[13] else None,
                        "vega": float(r[14] or 0) if r[14] else None,
                        "underlying_ltp": float(r[15] or 0) if r[15] else None,
                        "snapshot_at": str(r[16]),
                    }
                    for r in rows
                ],
                "expiry_date": expiry_date,
                "count": len(rows),
            }
    except Exception as e:
        return {"result": [], "error": str(e)}


async def execute_get_iv_context(params: dict, ctx: "ToolContext") -> dict:
    """Fetch IV history and HV for an underlying to assess IV richness."""
    underlying_id = params["underlying_id"]
    lookback_days = int(params.get("lookback_days", 30))

    try:
        async with ctx.db() as db:
            result = await db.execute(
                text("""
                    SELECT date, atm_iv, iv_rank_52w, iv_percentile_52w
                    FROM iv_history
                    WHERE instrument_id = :iid
                      AND date >= CURRENT_DATE - :days
                    ORDER BY date DESC
                    LIMIT 100
                """),
                {"iid": str(underlying_id), "days": lookback_days},
            )
            rows = result.fetchall()
            current = rows[0] if rows else None
            return {
                "current_atm_iv": float(current[1]) if current else None,
                "iv_rank_52w": float(current[2]) if current and current[2] else None,
                "iv_percentile_52w": float(current[3]) if current and current[3] else None,
                "iv_regime": _classify_iv(
                    float(current[2]) if current and current[2] else None
                ),
                "history": [
                    {
                        "date": str(r[0]),
                        "atm_iv": float(r[1]),
                        "iv_rank_52w": float(r[2] or 0),
                        "iv_percentile_52w": float(r[3] or 0),
                    }
                    for r in rows
                ],
                "count": len(rows),
            }
    except Exception as e:
        return {"result": [], "error": str(e)}


def _classify_iv(iv_rank_52w: float | None) -> str:
    if iv_rank_52w is None:
        return "unknown"
    if iv_rank_52w < 30:
        return "cheap"
    if iv_rank_52w < 60:
        return "fair"
    return "rich"


async def execute_enumerate_eligible_strategies(params: dict, ctx: "ToolContext") -> dict:
    """List F&O strategies eligible given direction, IV regime, and VIX regime."""
    direction = params["direction"]
    iv_regime = params["iv_regime"]
    vix_regime = params["vix_regime"]
    expiry_days = int(params["expiry_days"])

    key = (direction, iv_regime, vix_regime)
    strategies = _STRATEGY_TABLE.get(key, [])

    # Filter by expiry horizon — very short expiry (< 3 days) avoids time-decay-heavy strategies
    if expiry_days < 3:
        strategies = [s for s in strategies if "calendar" not in s and "condor" not in s]

    return {
        "strategies": strategies,
        "direction": direction,
        "iv_regime": iv_regime,
        "vix_regime": vix_regime,
        "expiry_days": expiry_days,
        "note": (
            "Defined-risk structures strongly preferred above VIX 18. "
            "Long premium favoured when iv_regime=cheap."
        ) if vix_regime == "high" else None,
    }


async def execute_get_strategy_payoff(params: dict, ctx: "ToolContext") -> dict:
    """Compute indicative payoff table for a specific options strategy.

    This is a pure-Python computation; no DB access required.
    """
    strategy_name = params["strategy_name"]
    legs = params["legs"]
    spot = float(params["spot"])
    iv_input = float(params["iv_input"])

    # Build indicative payoff grid ±20% around spot in 2% steps
    import math

    steps = [spot * (1 + pct / 100) for pct in range(-20, 22, 2)]
    payoff_grid = []

    for s in steps:
        leg_pnl = 0.0
        for leg in legs:
            strike = float(leg.get("strike", spot))
            opt_type = leg.get("type", "CE").upper()
            qty = int(leg.get("qty", 1))
            side = 1 if leg.get("side", "buy").lower() == "buy" else -1
            premium = float(leg.get("premium", 0))

            intrinsic = max(0.0, (s - strike) if opt_type == "CE" else (strike - s))
            leg_pnl += side * qty * (intrinsic - premium)

        payoff_grid.append({"spot_at_expiry": round(s, 2), "pnl": round(leg_pnl, 2)})

    max_profit = max(p["pnl"] for p in payoff_grid)
    max_loss = min(p["pnl"] for p in payoff_grid)
    breakevens = [
        p["spot_at_expiry"]
        for i, p in enumerate(payoff_grid[1:], 1)
        if payoff_grid[i - 1]["pnl"] * p["pnl"] < 0
    ]

    return {
        "strategy": strategy_name,
        "payoff_grid": payoff_grid,
        "max_profit": max_profit,
        "max_loss": max_loss,
        "breakevens": breakevens,
        "risk_reward": abs(max_profit / max_loss) if max_loss != 0 else None,
    }


async def execute_check_ban_list(params: dict, ctx: "ToolContext") -> dict:
    """Check whether an F&O instrument is on the SEBI ban list today."""
    instrument_id = params["instrument_id"]

    try:
        async with ctx.db() as db:
            result = await db.execute(
                text("""
                    SELECT ban_date, source
                    FROM fno_ban_list
                    WHERE instrument_id = :iid
                      AND ban_date = CURRENT_DATE
                    LIMIT 1
                """),
                {"iid": str(instrument_id)},
            )
            row = result.fetchone()
            is_banned = row is not None
            return {
                "is_banned": is_banned,
                "ban_date": str(row[0]) if row else None,
                "source": row[1] if row else None,
                "note": (
                    "New F&O positions are PROHIBITED in this instrument today. "
                    "Only closing existing positions is allowed."
                ) if is_banned else None,
            }
    except Exception as e:
        return {"is_banned": False, "error": str(e)}
