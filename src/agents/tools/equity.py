"""Equity tool executors: score_technicals, score_fundamentals, position_sizing."""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from src.agents.tools.registry import ToolContext


async def execute_score_technicals(params: dict, ctx: "ToolContext") -> dict:
    """Score the technical setup for an equity instrument.

    Computes a composite technical score from price_daily data:
    - Trend: price vs 20/50/200 EMA bands (proxied with SMA on daily close)
    - RSI(14) approximation from daily close returns
    - Volume confirmation: is today's volume > 20-day avg?
    - Breakout: close > 20-day high (flag)
    """
    instrument_id = params["instrument_id"]
    lookback_days = int(params.get("lookback_days", 60))

    try:
        async with ctx.db() as db:
            result = await db.execute(
                text("""
                    SELECT date, close, volume
                    FROM price_daily
                    WHERE instrument_id = :iid
                      AND date >= CURRENT_DATE - :days
                    ORDER BY date ASC
                """),
                {"iid": str(instrument_id), "days": lookback_days},
            )
            rows = result.fetchall()

        if len(rows) < 14:
            return {"score": None, "note": "insufficient price history"}

        closes = [float(r[1]) for r in rows]
        volumes = [int(r[2] or 0) for r in rows]

        # Simple moving averages
        def sma(series: list[float], n: int) -> float | None:
            if len(series) < n:
                return None
            return sum(series[-n:]) / n

        sma_20 = sma(closes, 20)
        sma_50 = sma(closes, 50)
        last_close = closes[-1]
        last_vol = volumes[-1]
        avg_vol_20 = sma(volumes, 20)

        # RSI(14) approximation
        gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
        losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
        avg_gain = sum(gains[-14:]) / 14
        avg_loss = sum(losses[-14:]) / 14
        rsi = 100 - (100 / (1 + avg_gain / avg_loss)) if avg_loss > 0 else 100.0

        # Score components (each 0-10)
        trend_score = 5.0
        if sma_20 and last_close > sma_20:
            trend_score += 2.0
        elif sma_20 and last_close < sma_20:
            trend_score -= 2.0
        if sma_50 and last_close > sma_50:
            trend_score += 2.0
        elif sma_50 and last_close < sma_50:
            trend_score -= 2.0
        trend_score = max(0.0, min(10.0, trend_score))

        rsi_score = 5.0
        if 40 <= rsi <= 65:
            rsi_score = 8.0
        elif rsi < 30:
            rsi_score = 9.0  # oversold — potential reversal
        elif rsi > 70:
            rsi_score = 2.0  # overbought
        rsi_score = max(0.0, min(10.0, rsi_score))

        volume_ok = avg_vol_20 and last_vol > avg_vol_20 * 1.1
        volume_score = 8.0 if volume_ok else 5.0

        breakout_20d = last_close >= max(closes[-20:]) if len(closes) >= 20 else False

        composite = (trend_score * 0.5 + rsi_score * 0.3 + volume_score * 0.2)

        return {
            "score": round(composite, 2),
            "components": {
                "trend_score": round(trend_score, 2),
                "rsi_score": round(rsi_score, 2),
                "volume_score": round(volume_score, 2),
            },
            "indicators": {
                "last_close": last_close,
                "sma_20": round(sma_20, 2) if sma_20 else None,
                "sma_50": round(sma_50, 2) if sma_50 else None,
                "rsi_14": round(rsi, 2),
                "volume_vs_20d_avg": round(last_vol / avg_vol_20, 2) if avg_vol_20 else None,
                "breakout_20d": breakout_20d,
            },
            "n_days_history": len(rows),
        }
    except Exception as e:
        return {"score": None, "error": str(e)}


async def execute_score_fundamentals(params: dict, ctx: "ToolContext") -> dict:
    """Score the fundamental valuation for an equity instrument.

    Uses instrument metadata (sector, market_cap) as a proxy since full
    financials are not yet in the DB. Returns a neutral score with notes.
    """
    instrument_id = params["instrument_id"]

    try:
        async with ctx.db() as db:
            result = await db.execute(
                text("""
                    SELECT symbol, sector, industry, market_cap_cr, is_fno
                    FROM instruments
                    WHERE id = :iid
                """),
                {"iid": str(instrument_id)},
            )
            row = result.fetchone()

        if not row:
            return {"score": None, "note": "instrument not found"}

        return {
            "score": 5.0,
            "symbol": row[0],
            "sector": row[1],
            "industry": row[2],
            "market_cap_cr": float(row[3]) if row[3] else None,
            "is_fno": row[4],
            "note": (
                "Fundamental data (P/E, EV/EBITDA, earnings growth) not yet "
                "available in DB — returning neutral score 5.0. "
                "Use news brief for qualitative fundamental context."
            ),
        }
    except Exception as e:
        return {"score": None, "error": str(e)}


async def execute_position_sizing(params: dict, ctx: "ToolContext") -> dict:
    """Compute recommended position size given conviction and risk params.

    Uses a simple Kelly-fraction-inspired formula capped at the conviction level:
        pct_of_book = conviction * (target_pct / (target_pct + abs(stop_pct))) * max_risk_pct
    Capped at 10% of book regardless of conviction.
    """
    account_value_inr = float(params["account_value_inr"])
    target_pct = float(params["target_pct"])
    stop_pct = abs(float(params["stop_pct"]))
    conviction = max(0.0, min(1.0, float(params["conviction"])))

    if stop_pct == 0:
        return {"error": "stop_pct must be non-zero"}

    # Kelly fraction: p*b - q / b where b = target/stop, p = conviction, q = 1-p
    b = target_pct / stop_pct
    kelly_fraction = (conviction * b - (1 - conviction)) / b
    kelly_fraction = max(0.0, kelly_fraction)

    # Conservative half-Kelly, capped at 10%
    half_kelly = kelly_fraction * 0.5
    position_pct = min(half_kelly * 100, 10.0)
    position_inr = account_value_inr * position_pct / 100

    max_loss_inr = position_inr * stop_pct / 100
    expected_gain_inr = position_inr * target_pct / 100

    return {
        "position_pct_of_book": round(position_pct, 2),
        "position_inr": round(position_inr, 0),
        "max_loss_inr": round(max_loss_inr, 0),
        "max_loss_pct_of_book": round(position_pct * stop_pct / 100, 3),
        "expected_gain_inr": round(expected_gain_inr, 0),
        "risk_reward": round(target_pct / stop_pct, 2),
        "conviction": conviction,
        "method": "half_kelly_capped_10pct",
    }
