"""Explorer-related tool executors: get_price_aggregates, get_past_predictions,
get_sentiment_history.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from src.agents.tools.registry import ToolContext

# Window definitions: (days_back, granularity_minutes)
_WINDOW_MAP = {
    "1d_daily_60":      (60,  1440),   # 60 days of daily bars
    "20d_hourly":       (20,  60),     # 20 days of hourly bars
    "5d_intraday_15m":  (5,   15),     # 5 days of 15-min bars
}


async def execute_get_price_aggregates(params: dict, ctx: "ToolContext") -> dict:
    """Fetch OHLCV aggregates for one instrument over a pre-defined window."""
    instrument_id = params["instrument_id"]
    window = params["window"]

    days_back, bucket_minutes = _WINDOW_MAP.get(window, (60, 1440))

    if bucket_minutes >= 1440:
        # Use price_daily for daily+ granularity
        try:
            async with ctx.db() as db:
                result = await db.execute(
                    text("""
                        SELECT date, open, high, low, close, volume, vwap, change_pct
                        FROM price_daily
                        WHERE instrument_id = :iid
                          AND date >= CURRENT_DATE - :days
                        ORDER BY date ASC
                        LIMIT 120
                    """),
                    {"iid": str(instrument_id), "days": days_back},
                )
                rows = result.fetchall()
                return {
                    "result": [
                        {
                            "ts": str(r[0]),
                            "open": float(r[1] or 0),
                            "high": float(r[2] or 0),
                            "low": float(r[3] or 0),
                            "close": float(r[4] or 0),
                            "volume": int(r[5] or 0),
                            "vwap": float(r[6] or 0) if r[6] else None,
                            "change_pct": float(r[7] or 0) if r[7] else None,
                        }
                        for r in rows
                    ],
                    "window": window,
                    "count": len(rows),
                }
        except Exception as e:
            return {"result": [], "error": str(e)}
    else:
        # Bucket price_ticks into intervals
        try:
            async with ctx.db() as db:
                result = await db.execute(
                    text("""
                        SELECT
                            date_trunc('minute', timestamp) -
                                INTERVAL '1 minute' * (EXTRACT(MINUTE FROM timestamp)::int % :bucket) AS bucket,
                            FIRST_VALUE(ltp) OVER w AS open,
                            MAX(high) OVER w AS high,
                            MIN(low) OVER w AS low,
                            LAST_VALUE(ltp) OVER w AS close,
                            SUM(volume) OVER w AS volume
                        FROM price_ticks
                        WHERE instrument_id = :iid
                          AND timestamp >= NOW() - :days * INTERVAL '1 day'
                        WINDOW w AS (
                            PARTITION BY date_trunc('minute', timestamp) -
                                INTERVAL '1 minute' * (EXTRACT(MINUTE FROM timestamp)::int % :bucket)
                            ORDER BY timestamp
                            ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
                        )
                        ORDER BY bucket ASC
                        LIMIT 500
                    """),
                    {"iid": str(instrument_id), "days": days_back, "bucket": bucket_minutes},
                )
                rows = result.fetchall()
                return {
                    "result": [
                        {
                            "ts": str(r[0]),
                            "open": float(r[1] or 0),
                            "high": float(r[2] or 0),
                            "low": float(r[3] or 0),
                            "close": float(r[4] or 0),
                            "volume": int(r[5] or 0),
                        }
                        for r in rows
                    ],
                    "window": window,
                    "count": len(rows),
                }
        except Exception as e:
            return {"result": [], "error": str(e)}


async def execute_get_past_predictions(params: dict, ctx: "ToolContext") -> dict:
    """Retrieve resolved past agent_predictions for an instrument or sector."""
    instrument_id = params.get("instrument_id")
    sector = params.get("sector")
    lookback_days = int(params.get("lookback_days", 90))
    only_resolved = bool(params.get("only_resolved", True))

    resolved_join = (
        "JOIN agent_predictions_outcomes apo ON apo.prediction_id = ap.id"
        if only_resolved
        else "LEFT JOIN agent_predictions_outcomes apo ON apo.prediction_id = ap.id"
    )

    bind: dict = {"days": lookback_days}
    where_clauses = ["ap.created_at >= NOW() - :days * INTERVAL '1 day'"]

    if instrument_id:
        where_clauses.append("ap.symbol_or_underlying = (SELECT symbol FROM instruments WHERE id = :iid LIMIT 1)")
        bind["iid"] = str(instrument_id)
    elif sector:
        where_clauses.append(
            "ap.symbol_or_underlying IN "
            "(SELECT symbol FROM instruments WHERE sector ILIKE :sector)"
        )
        bind["sector"] = f"%{sector}%"

    where_sql = " AND ".join(where_clauses)

    try:
        async with ctx.db() as db:
            result = await db.execute(
                text(f"""
                    SELECT ap.id, ap.symbol_or_underlying, ap.decision,
                           ap.conviction, ap.expected_pnl_pct,
                           ap.created_at, ap.prompt_versions,
                           apo.realised_pnl_pct, apo.hit_target, apo.hit_stop,
                           apo.exit_reason
                    FROM agent_predictions ap
                    {resolved_join}
                    WHERE {where_sql}
                    ORDER BY ap.created_at DESC
                    LIMIT 50
                """),
                bind,
            )
            rows = result.fetchall()
            return {
                "result": [
                    {
                        "id": str(r[0]),
                        "symbol": r[1],
                        "decision": r[2],
                        "conviction": float(r[3] or 0),
                        "expected_pnl_pct": float(r[4] or 0) if r[4] else None,
                        "created_at": str(r[5]),
                        "prompt_versions": r[6],
                        "realised_pnl_pct": float(r[7]) if r[7] is not None else None,
                        "hit_target": r[8],
                        "hit_stop": r[9],
                        "exit_reason": r[10],
                    }
                    for r in rows
                ],
                "count": len(rows),
            }
    except Exception as e:
        return {"result": [], "error": str(e)}


async def execute_get_sentiment_history(params: dict, ctx: "ToolContext") -> dict:
    """Fetch daily sentiment score time-series for one instrument."""
    instrument_id = params["instrument_id"]
    since = params["since"]
    granularity = params.get("granularity", "daily")

    trunc = "day" if granularity == "daily" else "week"

    try:
        async with ctx.db() as db:
            result = await db.execute(
                text(f"""
                    SELECT
                        date_trunc('{trunc}', s.signal_date) AS period,
                        COUNT(*) AS n_signals,
                        AVG(CASE WHEN s.action = 'BUY' THEN s.confidence
                                 WHEN s.action = 'SELL' THEN -s.confidence
                                 ELSE 0 END) AS sentiment_score,
                        SUM(CASE WHEN s.action = 'BUY' THEN 1 ELSE 0 END) AS n_buy,
                        SUM(CASE WHEN s.action = 'SELL' THEN 1 ELSE 0 END) AS n_sell,
                        SUM(CASE WHEN s.action = 'HOLD' THEN 1 ELSE 0 END) AS n_hold
                    FROM signals s
                    WHERE s.instrument_id = :iid
                      AND s.signal_date >= :since
                    GROUP BY period
                    ORDER BY period ASC
                """),
                {"iid": str(instrument_id), "since": since},
            )
            rows = result.fetchall()
            return {
                "result": [
                    {
                        "period": str(r[0]),
                        "n_signals": int(r[1] or 0),
                        "sentiment_score": float(r[2] or 0),
                        "n_buy": int(r[3] or 0),
                        "n_sell": int(r[4] or 0),
                        "n_hold": int(r[5] or 0),
                    }
                    for r in rows
                ],
                "granularity": granularity,
            }
    except Exception as e:
        return {"result": [], "error": str(e)}
