"""Prompt-enrichment helpers shared by equity and F&O LLM brains.

Every helper returns a *self-contained markdown block* that can be dropped
straight into a prompt. Each block is bounded in length so wide books or
busy lesson feeds do not crowd out the rest of the context. Failures
degrade to a one-line stub — a bad enrichment must never break the brain.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger
from sqlalchemy import text

from src.db import session_scope
from src.fno import LIVE_FNO_STATUSES


async def build_open_book_block(
    portfolio_id: uuid.UUID | str,
    *,
    as_of: datetime | None = None,
) -> str:
    """Markdown block listing every position the brain must respect.

    Renders three sections (each elided when empty):
      * ``EQUITY HOLDINGS`` — current quantity, avg buy price, days held.
      * ``OPEN F&O POSITIONS`` — strategy_type, expiry, entry premium,
        stop premium, days held.
      * ``OPEN F&O THESES BY UNDERLYING`` — one row per underlying with
        the directional sense (long_call → bullish, long_put → bearish)
        so the brain can spot opposing-leg traps.
    """
    as_of_eff = as_of or datetime.now(tz=timezone.utc)
    pid = str(portfolio_id)
    lines: list[str] = ["OPEN_BOOK (must check before proposing — do not duplicate or contradict):"]

    try:
        async with session_scope() as session:
            rows = list((await session.execute(
                text(
                    "SELECT i.symbol, h.quantity, h.avg_buy_price, "
                    "       h.first_buy_date "
                    "FROM holdings h JOIN instruments i "
                    "  ON i.id = h.instrument_id "
                    "WHERE h.portfolio_id = :pid AND h.quantity > 0 "
                    "ORDER BY i.symbol"
                ),
                {"pid": pid},
            )).all())
    except Exception as exc:
        logger.debug(f"build_open_book_block: holdings failed: {exc}")
        rows = []

    if rows:
        lines.append("")
        lines.append("EQUITY HOLDINGS:")
        lines.append("| symbol | qty | avg_buy | days_held |")
        for r in rows[:30]:
            days_held = (
                (as_of_eff.date() - r[3].date()).days if r[3] is not None else 0
            )
            lines.append(
                f"| {r[0]} | {int(r[1])} | {float(r[2]):.2f} | {days_held} |"
            )

    try:
        async with session_scope() as session:
            fno_rows = list((await session.execute(
                text(
                    "SELECT i.symbol, fs.strategy_type, fs.expiry_date, "
                    "       fs.entry_premium_net, fs.stop_premium_net, "
                    "       fs.target_premium_net, fs.filled_at, fs.proposed_at "
                    "FROM fno_signals fs JOIN instruments i "
                    "  ON i.id = fs.underlying_id "
                    "WHERE fs.status = ANY(:statuses) "
                    "  AND fs.dryrun_run_id IS NULL "
                    "ORDER BY fs.filled_at DESC NULLS LAST"
                ),
                {"statuses": list(LIVE_FNO_STATUSES)},
            )).all())
    except Exception as exc:
        logger.debug(f"build_open_book_block: fno failed: {exc}")
        fno_rows = []

    if fno_rows:
        lines.append("")
        lines.append("OPEN F&O POSITIONS:")
        lines.append("| symbol | strategy | expiry | entry_net | stop_net | days |")
        directional_by_symbol: dict[str, set[str]] = {}
        for r in fno_rows[:40]:
            filled = r[6] or r[7]
            days_held = (
                (as_of_eff.date() - filled.date()).days if filled else 0
            )
            entry = float(r[3]) if r[3] is not None else 0.0
            stop = float(r[4]) if r[4] is not None else 0.0
            lines.append(
                f"| {r[0]} | {r[1]} | "
                f"{r[2].isoformat() if r[2] else 'n/a'} | "
                f"{entry:.0f} | {stop:.0f} | {days_held} |"
            )
            sense = _direction_for_strategy(r[1])
            directional_by_symbol.setdefault(r[0], set()).add(sense)

        # Underlying-level directional summary helps the brain spot
        # synthetic-flat traps without re-parsing the table above.
        opposing = {
            sym: senses
            for sym, senses in directional_by_symbol.items()
            if len(senses) > 1 and {"bullish", "bearish"}.issubset(senses)
        }
        if opposing:
            lines.append("")
            lines.append("WARNING — opposing legs already open on these underlyings:")
            for sym, senses in sorted(opposing.items()):
                lines.append(f"  - {sym}: {sorted(senses)}")

    if len(lines) == 1:
        lines.append("(book is empty — fresh start)")

    return "\n".join(lines)


def _direction_for_strategy(strategy: str | None) -> str:
    """Coarse bullish/bearish/neutral tag from F&O strategy_type."""
    if not strategy:
        return "neutral"
    s = strategy.lower()
    if "call" in s and "spread" not in s and "bear" not in s:
        return "bullish"
    if "put" in s and "spread" not in s and "bull" not in s:
        return "bearish"
    if "bull" in s:
        return "bullish"
    if "bear" in s:
        return "bearish"
    return "neutral"


async def build_recent_outcomes_block(
    *,
    asset_class: str,
    window_days: int = 10,
    as_of: datetime | None = None,
) -> str:
    """Aggregated track record for self-calibration.

    For ``asset_class='EQUITY'`` reads from ``trades`` (closed trades only).
    For ``asset_class='FNO'`` reads from ``fno_signals.final_pnl``.
    """
    as_of_eff = as_of or datetime.now(tz=timezone.utc)
    since = as_of_eff - timedelta(days=window_days)
    asset_class = asset_class.upper()

    if asset_class == "EQUITY":
        return await _equity_outcomes(since, as_of_eff, window_days)
    return await _fno_outcomes(since, window_days)


async def _equity_outcomes(
    since: datetime, as_of: datetime, window_days: int
) -> str:
    """Realized P&L from closed equity trades within the window.

    Pairs BUY+SELL legs on (portfolio, instrument) using the SELL leg as the
    closure marker. Falls back to the trade-level ``pnl`` column when the
    closure linker has stamped it; otherwise computes per-symbol round-trips
    from the gross + cost columns directly.
    """
    try:
        async with session_scope() as session:
            row = (await session.execute(
                text(
                    "WITH legs AS (\n"
                    "  SELECT t.portfolio_id, t.instrument_id, t.trade_type,\n"
                    "         SUM(t.quantity*t.price) AS gross,\n"
                    "         SUM(COALESCE(t.brokerage,0)+COALESCE(t.stt,0)) AS costs,\n"
                    "         MIN(t.executed_at) AS first_ts,\n"
                    "         MAX(t.executed_at) AS last_ts\n"
                    "  FROM trades t\n"
                    "  WHERE t.executed_at >= :since AND t.executed_at <= :until\n"
                    "  GROUP BY 1,2,3\n"
                    "), pairs AS (\n"
                    "  SELECT b.portfolio_id, b.instrument_id,\n"
                    "         (s.gross - b.gross) - (b.costs + s.costs) AS net_pnl,\n"
                    "         (s.gross - b.gross) AS gross_pnl,\n"
                    "         b.gross AS notional\n"
                    "  FROM legs b JOIN legs s\n"
                    "    ON b.portfolio_id = s.portfolio_id\n"
                    "   AND b.instrument_id = s.instrument_id\n"
                    "  WHERE b.trade_type='BUY' AND s.trade_type='SELL'\n"
                    ")\n"
                    "SELECT COUNT(*) AS n,\n"
                    "       COUNT(*) FILTER (WHERE net_pnl > 0) AS wins,\n"
                    "       COALESCE(SUM(net_pnl),0) AS net,\n"
                    "       COALESCE(SUM(gross_pnl),0) AS gross,\n"
                    "       COALESCE(SUM(notional),0) AS notional\n"
                    "FROM pairs"
                ),
                {"since": since, "until": as_of},
            )).first()
    except Exception as exc:
        logger.debug(f"_equity_outcomes failed: {exc}")
        return f"RECENT EQUITY OUTCOMES ({window_days}d): unavailable"

    if not row or (row[0] or 0) == 0:
        return f"RECENT EQUITY OUTCOMES ({window_days}d): no closed round-trips on file"

    n, wins, net, gross, notional = row
    win_rate = (wins or 0) / max(int(n), 1)
    cost_pct_of_gross = (
        (float(gross) - float(net)) / abs(float(gross)) * 100.0
        if gross and float(gross) != 0 else None
    )
    return (
        f"RECENT EQUITY OUTCOMES (last {window_days}d, {n} closed round-trips):\n"
        f"  - Net P&L: Rs{float(net):.2f}  |  Gross P&L: Rs{float(gross):.2f}\n"
        f"  - Hit rate: {wins}/{n} = {win_rate*100:.0f}%\n"
        + (
            f"  - Costs ate {cost_pct_of_gross:.0f}% of gross — "
            "use this to set your minimum expected-move threshold.\n"
            if cost_pct_of_gross is not None and cost_pct_of_gross > 0
            else ""
        )
        + "  Lesson: if expected_move_pct < 2 * cost_pct_per_trade, skip the trade."
    )


async def _fno_outcomes(since: datetime, window_days: int) -> str:
    """Win-rate + P&L breakdown by strategy_type from closed F&O signals."""
    try:
        async with session_scope() as session:
            rows = list((await session.execute(
                text(
                    "SELECT strategy_type,\n"
                    "       COUNT(*) AS n,\n"
                    "       COUNT(*) FILTER (WHERE final_pnl > 0) AS wins,\n"
                    "       COALESCE(SUM(final_pnl),0) AS pnl_sum,\n"
                    "       COALESCE(MIN(final_pnl),0) AS worst,\n"
                    "       COALESCE(MAX(final_pnl),0) AS best\n"
                    "FROM fno_signals\n"
                    "WHERE closed_at >= :since\n"
                    "  AND final_pnl IS NOT NULL\n"
                    "  AND dryrun_run_id IS NULL\n"
                    "GROUP BY 1\n"
                    "ORDER BY pnl_sum ASC"
                ),
                {"since": since},
            )).all())
    except Exception as exc:
        logger.debug(f"_fno_outcomes failed: {exc}")
        return f"RECENT F&O OUTCOMES ({window_days}d): unavailable"

    if not rows:
        return f"RECENT F&O OUTCOMES ({window_days}d): no closed signals on file"

    lines = [
        f"RECENT F&O OUTCOMES (last {window_days}d, by strategy_type):",
        "| strategy | n | wins | hit% | net_pnl | worst | best |",
    ]
    total = 0.0
    for r in rows:
        n = int(r[1])
        wins = int(r[2])
        net = float(r[3])
        worst = float(r[4])
        best = float(r[5])
        total += net
        lines.append(
            f"| {r[0]} | {n} | {wins} | "
            f"{(wins/max(n,1))*100:.0f}% | "
            f"{net:.0f} | {worst:.0f} | {best:.0f} |"
        )
    lines.append(f"  Net across all strategies: Rs{total:.0f}")
    lines.append(
        "  Lesson: do not increase exposure to a strategy_type whose net "
        "is negative across this window without a regime change."
    )
    return "\n".join(lines)


async def build_lessons_block(
    *,
    asset_class: str,
    limit: int = 8,
    lookback_days: int = 60,
    as_of: datetime | None = None,
) -> str:
    """Recent active lessons for this asset_class as a numbered list.

    Lessons are appended to ``strategy_lessons`` after notable sessions and
    surfaced here so the LLM has its own failure modes in context. Returns
    an empty-string sentinel if the table is empty (caller can drop the
    block) — never raises.
    """
    as_of_eff = as_of or datetime.now(tz=timezone.utc)
    since = (as_of_eff - timedelta(days=lookback_days)).date()
    asset_class = asset_class.upper()

    try:
        async with session_scope() as session:
            rows = list((await session.execute(
                text(
                    "SELECT lesson_date, severity, title, body "
                    "FROM strategy_lessons "
                    "WHERE is_active "
                    "  AND lesson_date >= :since "
                    "  AND asset_class IN (:ac, 'BOTH') "
                    "ORDER BY "
                    "  CASE severity "
                    "    WHEN 'blocking' THEN 0 "
                    "    WHEN 'major' THEN 1 "
                    "    ELSE 2 END, "
                    "  lesson_date DESC "
                    "LIMIT :lim"
                ),
                {"ac": asset_class, "since": since, "lim": limit},
            )).all())
    except Exception as exc:
        logger.debug(f"build_lessons_block failed: {exc}")
        return ""

    if not rows:
        return ""

    # Cap each rendered lesson at ~280 chars body so the block stays bounded
    # even as the lessons table grows. Truncation is suffix-style ("…") so
    # the lead sentence — which carries the rule — is always preserved.
    _LESSON_BODY_CAP = 280
    lines = [
        f"LESSONS FROM PRIOR SESSIONS ({asset_class}, last {lookback_days}d) — "
        "do not repeat:",
    ]
    for i, r in enumerate(rows, 1):
        lesson_date, severity, title, body = r
        body_clean = (body or "").strip().replace("\n", " ")
        if len(body_clean) > _LESSON_BODY_CAP:
            body_clean = body_clean[: _LESSON_BODY_CAP - 1].rstrip() + "…"
        lines.append(
            f"{i}. [{severity.upper()}] {title} ({lesson_date.isoformat()})\n"
            f"   {body_clean}"
        )
    return "\n".join(lines)


async def build_full_enrichment(
    *,
    portfolio_id: uuid.UUID | str,
    asset_class: str,
    as_of: datetime | None = None,
    outcomes_window_days: int = 10,
    lessons_lookback_days: int = 60,
    lessons_limit: int = 8,
) -> str:
    """Convenience: stitch open_book + recent outcomes + lessons into one block.

    Callers that only want a subset can call the individual builders.
    """
    parts: list[str] = []
    parts.append(await build_open_book_block(portfolio_id, as_of=as_of))
    parts.append(
        await build_recent_outcomes_block(
            asset_class=asset_class,
            window_days=outcomes_window_days,
            as_of=as_of,
        )
    )
    lessons = await build_lessons_block(
        asset_class=asset_class,
        limit=lessons_limit,
        lookback_days=lessons_lookback_days,
        as_of=as_of,
    )
    if lessons:
        parts.append(lessons)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Hard-rule reference text (shared by prompt builders + post-LLM validators).
# Keep the wording aligned: the validator checks the *intent* the prompt
# describes, so changing one without the other creates a silent drift.
# ---------------------------------------------------------------------------

EQUITY_HARD_RULES = """\
EQUITY HARD RULES (violations are auto-skipped by the gate; do not propose them):
1. SUB-SCALE FRICTION: when current_cash < Rs 200,000, expected move
   (target-entry)/entry must be >= 2.0% AND confidence >= 0.75. Skip otherwise.
2. HIGH-VIX REGIME (vix_regime='high' OR vix_value >= 17):
   - Required confidence to ENTER >= 0.75 (no exceptions).
   - Max 2 new entries per morning_allocation cycle.
   - Each entry must have a thesis that survives an overnight gap; if you
     intend to flatten at EOD, do not enter — the costs eat the edge.
3. NO-OVERNIGHT BAN IS NOT BLANKET: do not blanket-flatten the entire book at
   EOD. Square off only positions where confidence < 0.75 OR pnl_pct in
   [-0.5%, +1%]. Strong-conviction holds (confidence >= 0.80) ride overnight.
4. PORTFOLIO-AWARE: do not propose BUY for a symbol already held (use the
   existing position) and do not propose SELL for a symbol you do not hold.
5. SECTOR CAP: do not push any sector above 35% of NAV (60% if
   risk_profile='aggressive')."""


FNO_HARD_RULES = """\
F&O HARD RULES (violations are auto-rejected by the gate; do not propose them):
1. REGIME GATE: when VIX >= 17 OR iv_regime in ('high','elevated'):
   - Do NOT propose naked long_call or long_put.
   - Prefer bull_call_spread / bear_put_spread (debit) or short_strangle /
     iron_condor when iv_percentile > 70 and event risk is low.
2. STOP DISCIPLINE: stop_premium_net must satisfy
   (entry_premium_net - stop_premium_net) / entry_premium_net <= 0.45.
   A stop at <55% of entry premium is a full-decay trade dressed up as a stop.
3. PORTFOLIO-AWARE: before proposing, read OPEN_BOOK above. Reject:
   (a) same strategy_type already open on this underlying+expiry,
   (b) opposing direction (long_call vs long_put) on same underlying+expiry —
       net synthetic-flat, paying theta on both sides.
4. CONCENTRATION: max 4 new F&O proposals per cycle. If proposing >6 names
   with similar bullish thesis, replace with one NIFTY/BANKNIFTY option of
   equivalent notional — cheaper spreads, same exposure.
5. THESIS DURABILITY: for long premium with days_held >= 1 and no fresh
   catalyst, prefer SELL (theta bleed) over HOLD."""
