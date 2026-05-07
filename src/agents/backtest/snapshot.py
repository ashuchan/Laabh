"""Market snapshot — read-only view of DB state at a chosen `as_of` date.

Used by the backtest runner to:
  1. Show the operator what the workflow's effective inputs would have been.
  2. Provide ground-truth price moves (open / close / high / low) on the same
     date so the report can do the "would the prediction have worked?" pass.

Pure reads — never writes. Safe to run against the live DB.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import text

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class MarketSnapshot:
    """Snapshot of relevant market state at `as_of`.

    "Inputs" = what the workflow at `as_of` morning would have seen.
    "Actuals" = how the day actually played out (for backtest scoring).
    """

    as_of: datetime
    target_date: date

    # Inputs (pre-market visible)
    vix_latest: float | None = None
    vix_observed_at: datetime | None = None
    nifty_prev_close: float | None = None
    raw_content_count_24h: int = 0
    signals_count_24h: int = 0
    bullish_signals_24h: int = 0
    bearish_signals_24h: int = 0
    top_movers_yesterday: list[dict] = field(default_factory=list)
    open_positions: list[dict] = field(default_factory=list)
    universe_sample: list[dict] = field(default_factory=list)
    yesterday_outcomes: list[dict] = field(default_factory=list)

    # Actuals (target date EOD — for scoring after the fact)
    actuals: dict[str, dict] = field(default_factory=dict)
    """{symbol: {open, high, low, close, prev_close, change_pct, volume}}"""

    top_signal_symbols: list[dict] = field(default_factory=list)
    """[{symbol, n_signals, n_buy, n_sell, recent_actions[]}] in the 24h pre-morning window."""

    fetch_ok: bool = False
    fetch_error: str | None = None

    def to_brain_triage_packet(self) -> dict:
        """Serialise the snapshot in the rough shape Brain Triage expects.

        This is a *minimal* packet — the goal is to make the prompt look
        plausible to mock and live LLMs alike. Production prompts pull richer
        data via tool calls; the backtest passes a static snapshot instead.
        """
        return {
            "as_of": self.as_of.isoformat(),
            "market_regime": {
                "vix": self.vix_latest,
                "vix_regime": self._vix_regime(),
                "nifty_prev_close": self.nifty_prev_close,
            },
            "universe": self.universe_sample,
            "signal_velocity": {
                "total_24h": self.signals_count_24h,
                "bullish": self.bullish_signals_24h,
                "bearish": self.bearish_signals_24h,
            },
            "top_movers": self.top_movers_yesterday,
            "top_signal_symbols": self.top_signal_symbols,
            "open_positions": self.open_positions,
            "yesterday_outcomes": self.yesterday_outcomes,
            "raw_content_volume_24h": self.raw_content_count_24h,
            "today_calendar": {
                "results_today": [],
                "rbi_today": False,
                "fomc_tonight": False,
                "ex_dates": [],
                "geopolitical_flags": [],
            },
            "cost_budget_remaining_usd": 5.0,
        }

    def _vix_regime(self) -> str:
        if self.vix_latest is None:
            return "unknown"
        if self.vix_latest < 12:
            return "low"
        if self.vix_latest > 18:
            return "high"
        return "neutral"


async def fetch_snapshot(
    target_date: date,
    db_session_factory,
    *,
    universe_size: int = 30,
) -> MarketSnapshot:
    """Pull a backtest snapshot for `target_date` from the live DB.

    Inputs use data with `created_at < target_date 09:00 IST` so we don't peek
    at the same day's news. Actuals use `price_daily` rows for `target_date`
    itself — that's the ground truth used for backtest P&L estimation.
    """
    morning_ist = datetime.combine(target_date, time(9, 0), tzinfo=IST)
    morning_utc = morning_ist.astimezone(timezone.utc).replace(tzinfo=None)
    yesterday = target_date - timedelta(days=1)
    five_days_ago = target_date - timedelta(days=5)

    snap = MarketSnapshot(as_of=morning_ist, target_date=target_date)

    try:
        async with db_session_factory() as db:
            # VIX (latest before target morning)
            vix_row = await db.execute(
                text("""
                    SELECT vix_value, timestamp FROM vix_ticks
                    WHERE timestamp < :morning
                    ORDER BY timestamp DESC LIMIT 1
                """),
                {"morning": morning_utc},
            )
            vix = vix_row.fetchone()
            if vix:
                snap.vix_latest = float(vix[0])
                snap.vix_observed_at = vix[1]

            # NIFTY previous close (best-effort: instrument symbol "NIFTY" or "NIFTY 50")
            nifty_row = await db.execute(
                text("""
                    SELECT pd.close FROM price_daily pd
                    JOIN instruments i ON pd.instrument_id = i.id
                    WHERE i.symbol IN ('NIFTY', 'NIFTY 50', 'NIFTY50', '^NSEI')
                      AND pd.date <= :prev_day
                    ORDER BY pd.date DESC LIMIT 1
                """),
                {"prev_day": yesterday},
            )
            nifty = nifty_row.fetchone()
            if nifty:
                snap.nifty_prev_close = float(nifty[0])

            # Raw content / signals volume in last 24h before morning
            content_count = await db.execute(
                text("""
                    SELECT COUNT(*) FROM raw_content
                    WHERE fetched_at >= :since AND fetched_at < :morning
                """),
                {"since": morning_utc - timedelta(hours=24), "morning": morning_utc},
            )
            snap.raw_content_count_24h = int(content_count.scalar() or 0)

            sig_rows = await db.execute(
                text("""
                    SELECT action, COUNT(*)
                    FROM signals
                    WHERE created_at >= :since AND created_at < :morning
                    GROUP BY action
                """),
                {"since": morning_utc - timedelta(hours=24), "morning": morning_utc},
            )
            for rec, n in sig_rows.fetchall():
                n_int = int(n)
                snap.signals_count_24h += n_int
                rec_u = (str(rec) if rec is not None else "").upper()
                if rec_u in {"BUY", "BULLISH", "STRONG_BUY"}:
                    snap.bullish_signals_24h += n_int
                elif rec_u in {"SELL", "BEARISH", "STRONG_SELL"}:
                    snap.bearish_signals_24h += n_int

            # Top movers — yesterday's top gainers/losers. Use stored
            # change_pct if available; many index/equity rows in this DB ship
            # with change_pct NULL, so derive `(close - open) / open` as a
            # fallback so the brain has something to look at.
            mover_rows = await db.execute(
                text("""
                    SELECT i.symbol, pd.open, pd.close, pd.change_pct, pd.volume
                    FROM price_daily pd
                    JOIN instruments i ON pd.instrument_id = i.id
                    WHERE pd.date = :prev_day
                """),
                {"prev_day": yesterday},
            )
            movers: list[dict] = []
            for r in mover_rows.fetchall():
                stored_chg = float(r[3]) if r[3] is not None else None
                if stored_chg is None and r[1] and r[2] and float(r[1]):
                    stored_chg = round((float(r[2]) - float(r[1])) / float(r[1]) * 100, 4)
                if stored_chg is None or abs(stored_chg) < 0.001:
                    continue
                movers.append({
                    "symbol": r[0],
                    "open": float(r[1]) if r[1] else None,
                    "close": float(r[2]) if r[2] else None,
                    "change_pct": stored_chg,
                    "volume": int(r[4]) if r[4] else None,
                    "driver": f"day move {stored_chg:+.2f}%",
                })
            movers.sort(key=lambda m: abs(m["change_pct"]), reverse=True)
            snap.top_movers_yesterday = movers[:10]

            # Universe sample — instruments with recent activity (signals or watchlist)
            univ_rows = await db.execute(
                text("""
                    SELECT i.id, i.symbol, i.sector, i.exchange,
                           pd.close, pd.change_pct,
                           COALESCE(sc.signal_count, 0) AS signal_count
                    FROM instruments i
                    LEFT JOIN price_daily pd
                      ON pd.instrument_id = i.id AND pd.date = :prev_day
                    LEFT JOIN (
                        SELECT instrument_id, COUNT(*) AS signal_count
                        FROM signals
                        WHERE created_at >= :since AND created_at < :morning
                        GROUP BY instrument_id
                    ) sc ON sc.instrument_id = i.id
                    WHERE i.is_active = true
                    ORDER BY COALESCE(sc.signal_count, 0) DESC, ABS(pd.change_pct) DESC NULLS LAST
                    LIMIT :n
                """),
                {"prev_day": yesterday,
                 "since": morning_utc - timedelta(hours=24),
                 "morning": morning_utc,
                 "n": universe_size},
            )
            snap.universe_sample = [
                {"instrument_id": str(r[0]), "symbol": r[1], "sector": r[2],
                 "exchange": r[3],
                 "current_price": float(r[4]) if r[4] else None,
                 "day_change_pct": float(r[5]) if r[5] else None,
                 "signals_24h_count": int(r[6])}
                for r in univ_rows.fetchall()
            ]

            # Top signal symbols — names with the most analyst chatter in the
            # last 24h before the morning. This is the single most useful
            # input for brain_triage besides regime: tells it *which symbols*
            # have catalysts to triage, with action breakdown and recency.
            top_sig_rows = await db.execute(
                text("""
                    SELECT i.symbol, i.sector, COUNT(*) AS n,
                           SUM((s.action::text = 'BUY')::int)  AS n_buy,
                           SUM((s.action::text = 'SELL')::int) AS n_sell,
                           SUM((s.action::text = 'HOLD')::int) AS n_hold,
                           MAX(s.confidence) AS max_conf,
                           ARRAY_AGG(DISTINCT s.analyst_name_raw)
                               FILTER (WHERE s.analyst_name_raw IS NOT NULL) AS analysts
                    FROM signals s
                    JOIN instruments i ON s.instrument_id = i.id
                    WHERE s.created_at >= :since AND s.created_at < :morning
                    GROUP BY i.symbol, i.sector
                    HAVING COUNT(*) >= 2
                    ORDER BY n DESC
                    LIMIT 20
                """),
                {"since": morning_utc - timedelta(hours=24), "morning": morning_utc},
            )
            snap.top_signal_symbols = [
                {
                    "symbol": r[0],
                    "sector": r[1],
                    "n_signals": int(r[2]),
                    "n_buy": int(r[3] or 0),
                    "n_sell": int(r[4] or 0),
                    "n_hold": int(r[5] or 0),
                    "max_confidence": float(r[6]) if r[6] else None,
                    "analysts": [a for a in (r[7] or []) if a][:5],
                }
                for r in top_sig_rows.fetchall()
            ]

            # Open positions — pull current holdings as proxy
            holdings_rows = await db.execute(
                text("""
                    SELECT i.symbol, h.quantity, h.avg_buy_price, h.current_price
                    FROM holdings h
                    JOIN instruments i ON h.instrument_id = i.id
                    WHERE h.quantity > 0
                    LIMIT 20
                """),
            )
            snap.open_positions = [
                {"symbol": r[0], "quantity": int(r[1]) if r[1] else 0,
                 "avg_price": float(r[2]) if r[2] else None,
                 "current_price": float(r[3]) if r[3] else None}
                for r in holdings_rows.fetchall()
            ]

            # Actuals — target_date OHLC for every universe symbol so we can
            # score predictions against real moves
            actual_rows = await db.execute(
                text("""
                    SELECT i.symbol, pd.open, pd.high, pd.low, pd.close,
                           pd.prev_close, pd.change_pct, pd.volume
                    FROM price_daily pd
                    JOIN instruments i ON pd.instrument_id = i.id
                    WHERE pd.date = :target_date
                """),
                {"target_date": target_date},
            )
            for r in actual_rows.fetchall():
                snap.actuals[r[0]] = {
                    "open": float(r[1]) if r[1] else None,
                    "high": float(r[2]) if r[2] else None,
                    "low": float(r[3]) if r[3] else None,
                    "close": float(r[4]) if r[4] else None,
                    "prev_close": float(r[5]) if r[5] else None,
                    "change_pct": float(r[6]) if r[6] else None,
                    "volume": int(r[7]) if r[7] else None,
                }

        snap.fetch_ok = True
    except Exception as e:
        log.warning("MarketSnapshot fetch failed: %s", e)
        snap.fetch_error = str(e)
    return snap
