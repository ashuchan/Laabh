"""
Analyst scorer service — backtests analyst signals via NautilusTrader
and updates the analyst_scoreboard table.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import text

from src.db import session_scope
from src.integrations.nautilus.backtester import run_signal_backtest

logger = logging.getLogger(__name__)


async def compute_analyst_backtest_score(
    analyst_id: str, lookback_days: int = 90
) -> dict:
    """
    Backtests all signals from an analyst over the last N days.
    Updates analyst_scoreboard table with backtested accuracy.

    Fails gracefully per-analyst: one bad signal does not abort the batch,
    and outer DB/unexpected errors are caught and reported via Telegram.

    Args:
        analyst_id: UUID string of the analyst
        lookback_days: number of days of signal history to backtest

    Returns:
        Summary dict with avg_return and win_rate (or error key on failure).
    """
    try:
        signals = await _fetch_analyst_signals(analyst_id, lookback_days)

        if not signals:
            logger.info(f"analyst_scorer: no signals for {analyst_id} in last {lookback_days}d")
            return {"analyst_id": analyst_id, "signal_count": 0,
                    "avg_return": None, "win_rate": None}

        results = []
        for sig in signals:
            try:
                ohlcv = await _fetch_ohlcv(sig["ticker"], sig["date"], days_forward=30)
                backtest = run_signal_backtest([sig], ohlcv, sig["ticker"])
                if "error" not in backtest:
                    results.append(backtest)
                else:
                    logger.warning(
                        f"analyst_scorer: backtest error for {sig['ticker']}: "
                        f"{backtest['error']}"
                    )
            except Exception as exc:
                logger.error(
                    f"analyst_scorer: signal backtest failed for {sig['ticker']}: {exc}"
                )
                continue  # skip this signal, keep scoring others

        avg_return = (
            sum(
                r["total_return_pct"]
                for r in results
                if r.get("total_return_pct") is not None
            )
            / len(results)
            if results
            else None
        )
        win_rate = (
            sum(r["win_rate"] for r in results if r.get("win_rate") is not None)
            / len(results)
            if results
            else None
        )

        await _update_analyst_score(
            analyst_id,
            {
                "backtested_return_pct": avg_return,
                "backtested_win_rate": win_rate,
                "signal_count": len(results),
                "scored_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        logger.info(
            f"analyst_scorer: {analyst_id} — {len(results)} signals, "
            f"avg_return={avg_return}, win_rate={win_rate}"
        )
        return {
            "analyst_id": analyst_id,
            "avg_return": avg_return,
            "win_rate": win_rate,
            "signal_count": len(results),
        }

    except Exception as exc:
        logger.error(
            f"analyst_scorer: compute_analyst_backtest_score failed for {analyst_id}: {exc}",
            exc_info=True,
        )
        try:
            from src.services.notification_service import NotificationService
            await NotificationService().send_text(
                f"⚠️ Sunday scorer failed for analyst `{analyst_id}`\n"
                f"Error: {exc}\n"
                "Scores not updated this week."
            )
        except Exception:
            pass  # Telegram failure must not cascade
        return {"analyst_id": analyst_id, "error": str(exc),
                "avg_return": None, "win_rate": None}


async def compute_analyst_backtest_score_all(lookback_days: int = 90) -> list[dict]:
    """
    Score all analysts. Runs per-analyst scoring independently so one failure
    does not abort the batch. Sends a summary Telegram message on completion.
    """
    analyst_ids = await _fetch_active_analyst_ids(lookback_days)
    results = []
    for analyst_id in analyst_ids:
        result = await compute_analyst_backtest_score(analyst_id, lookback_days)
        results.append(result)

    successes = [r for r in results if "error" not in r]
    failures = [r for r in results if "error" in r]

    summary = (
        f"📊 Sunday Analyst Scoring Complete\n"
        f"✅ Scored: {len(successes)} analysts\n"
        f"❌ Failed: {len(failures)} analysts"
    )
    if failures:
        summary += "\n\nFailed analysts:\n" + "\n".join(
            f"  • {r['analyst_id']}: {r.get('error', 'unknown')}"
            for r in failures
        )

    try:
        from src.services.notification_service import NotificationService
        await NotificationService().send_text(summary)
    except Exception:
        pass

    logger.info(f"analyst_scorer: processed {len(results)} analysts")
    return results


async def _fetch_analyst_signals(analyst_id: str, lookback_days: int) -> list[dict]:
    """Fetch signals attributed to the given analyst."""
    async with session_scope() as session:
        rows = await session.execute(
            text("""
                SELECT s.id, i.symbol AS ticker, s.created_at::date AS date,
                       s.action AS direction, s.target_price, s.stop_loss
                FROM signals s
                JOIN instruments i ON i.id = s.instrument_id
                WHERE s.analyst_id = :analyst_id
                  AND s.created_at >= NOW() - make_interval(days => :days)
                ORDER BY s.created_at
            """),
            {"analyst_id": analyst_id, "days": lookback_days},
        )
        return [dict(r._mapping) for r in rows.fetchall()]


async def _fetch_ohlcv(ticker: str, start_date, days_forward: int) -> pd.DataFrame:
    """Fetch OHLCV data for backtesting from the price_daily table."""
    async with session_scope() as session:
        rows = await session.execute(
            text("""
                SELECT date, open, high, low, close, volume
                FROM price_daily
                JOIN instruments i ON i.id = price_daily.instrument_id
                WHERE i.symbol = :symbol
                  AND date >= :start_date
                  AND date <= :start_date + make_interval(days => :days)
                ORDER BY date
            """),
            {"symbol": ticker, "start_date": str(start_date), "days": days_forward},
        )
        records = rows.fetchall()
    return pd.DataFrame(records, columns=["date", "open", "high", "low", "close", "volume"])


async def _update_analyst_score(analyst_id: str, scores: dict) -> None:
    """Upsert analyst backtest scores into analyst_scoreboard."""
    async with session_scope() as session:
        await session.execute(
            text("""
                INSERT INTO analyst_scoreboard
                    (analyst_id, backtested_return_pct, backtested_win_rate,
                     signal_count, scored_at)
                VALUES
                    (:analyst_id, :backtested_return_pct, :backtested_win_rate,
                     :signal_count, :scored_at)
                ON CONFLICT (analyst_id) DO UPDATE SET
                    backtested_return_pct = EXCLUDED.backtested_return_pct,
                    backtested_win_rate   = EXCLUDED.backtested_win_rate,
                    signal_count          = EXCLUDED.signal_count,
                    scored_at             = EXCLUDED.scored_at
            """),
            {
                "analyst_id": analyst_id,
                "backtested_return_pct": scores["backtested_return_pct"],
                "backtested_win_rate": scores["backtested_win_rate"],
                "signal_count": scores["signal_count"],
                "scored_at": scores["scored_at"],
            },
        )
        await session.commit()


async def _fetch_active_analyst_ids(lookback_days: int) -> list[str]:
    """Return IDs of analysts who have signals in the lookback window."""
    async with session_scope() as session:
        rows = await session.execute(
            text("""
                SELECT DISTINCT analyst_id::text
                FROM signals
                WHERE analyst_id IS NOT NULL
                  AND created_at >= NOW() - make_interval(days => :days)
            """),
            {"days": lookback_days},
        )
        return [r[0] for r in rows.fetchall()]
