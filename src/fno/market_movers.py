"""Top F&O underlying movers for the previous trading day.

Used by Phase 3 thesis synthesis to give the LLM a market-regime cue:
which F&O-listed names led/lagged on the prior session, with absolute
and percentage moves. Awareness-only — does not expand the candidate
universe (Phase 1/2 still gate that).

Data source is the NSE bhavcopy archive via :mod:`src.dryrun.bhavcopy`,
which already disk-caches results, so repeat calls within a session
are free.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytz
from loguru import logger

from src.dryrun.bhavcopy import (
    BhavcopyMissingError,
    fetch_cm_bhavcopy,
    fetch_fo_bhavcopy,
)

_IST = pytz.timezone("Asia/Kolkata")
# How many trading days to walk back when looking for a non-404 archive.
# Covers a long-weekend + a single mid-week holiday without giving up.
_MAX_HOLIDAY_WALKBACK = 5


@dataclass(frozen=True)
class Mover:
    """One F&O underlying's daily move, ready for prompt rendering."""

    symbol: str
    prev_close: Decimal
    close: Decimal
    change_abs: Decimal
    pct_change: float
    rank: int  # 1-indexed within its list (top or bottom)


@dataclass(frozen=True)
class MarketMovers:
    """Top gainers + bottom losers among F&O underlyings for ``as_of_date``."""

    as_of_date: date
    top: list[Mover]
    bottom: list[Mover]

    def by_symbol(self) -> dict[str, Mover]:
        """Return a {symbol: Mover} map across both lists for O(1) lookup."""
        out: dict[str, Mover] = {}
        for m in (*self.top, *self.bottom):
            out[m.symbol] = m
        return out


def _resolve_target_date(as_of: datetime | None) -> date:
    """Map ``as_of`` to the previous calendar day in IST.

    Holiday/weekend skipping is left to the bhavcopy walkback in
    :func:`get_top_fno_movers` — this just picks a reasonable starting point.
    """
    if as_of is None:
        as_of = datetime.now(tz=timezone.utc)
    as_of_ist = as_of.astimezone(_IST) if as_of.tzinfo else _IST.localize(as_of)
    return as_of_ist.date() - timedelta(days=1)


def _previous_calendar_day(d: date) -> date:
    return d - timedelta(days=1)


async def get_top_fno_movers(
    *,
    top_n: int = 10,
    bottom_n: int = 5,
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,  # noqa: ARG001 — accepted for convention
) -> MarketMovers:
    """Return top gainers + bottom losers among F&O underlyings.

    Walks back from ``as_of - 1 day`` (IST) through the bhavcopy archive
    until a non-404 file is found, so weekend/holiday calls Just Work.
    Cross-references the F&O bhavcopy (universe) with the cash-market
    bhavcopy (prices) and ranks by ``(close - prev_close) / prev_close``.

    The ``dryrun_run_id`` parameter is accepted for the CLAUDE.md
    pipeline convention but unused — this function reads only the public
    bhavcopy archive, which is identical for live and dryrun runs.
    """
    target = _resolve_target_date(as_of)
    fo_df = None
    cm_df = None
    final_date: date = target
    for _ in range(_MAX_HOLIDAY_WALKBACK):
        try:
            fo_df = await fetch_fo_bhavcopy(target)
            cm_df = await fetch_cm_bhavcopy(target)
            final_date = target
            break
        except BhavcopyMissingError as exc:
            logger.debug(
                f"market_movers: bhavcopy 404 for {target} ({exc}); "
                "trying day before"
            )
            target = _previous_calendar_day(target)

    if fo_df is None or cm_df is None or fo_df.empty or cm_df.empty:
        logger.warning(
            f"market_movers: no bhavcopy available within "
            f"{_MAX_HOLIDAY_WALKBACK} days of as_of={as_of}"
        )
        return MarketMovers(as_of_date=final_date, top=[], bottom=[])

    fno_universe = set(
        fo_df["symbol"].dropna().astype(str).str.upper().str.strip().unique()
    )

    cm = cm_df
    if "instrument_type" in cm.columns:
        cm = cm[cm["instrument_type"].astype(str).str.strip().str.upper() == "STK"]
    if "series" in cm.columns:
        cm = cm[cm["series"].astype(str).str.strip().str.upper() == "EQ"]
    # Take a fresh copy here — the prior boolean filters can return views,
    # and the upcoming column assignment would otherwise raise
    # SettingWithCopyWarning (or error in pandas 3.x).
    cm = cm.copy()
    cm["symbol"] = cm["symbol"].astype(str).str.upper().str.strip()
    cm = cm[cm["symbol"].isin(fno_universe)]
    cm = cm.dropna(subset=["close", "prev_close"])
    cm = cm[cm["prev_close"] > 0]

    if cm.empty:
        logger.warning(
            f"market_movers: bhavcopy for {final_date} had no "
            "F&O EQ underlyings with valid close/prev_close"
        )
        return MarketMovers(as_of_date=final_date, top=[], bottom=[])

    cm = cm.assign(
        pct_change=(cm["close"] - cm["prev_close"]) / cm["prev_close"] * 100.0
    )

    def _to_movers(rows, *, ascending: bool) -> list[Mover]:
        ordered = rows.sort_values("pct_change", ascending=ascending)
        out: list[Mover] = []
        for i, (_, r) in enumerate(ordered.iterrows(), 1):
            # str(numpy_float) gives a clean repr we can hand straight to
            # Decimal — no float() round-trip, which would discard precision.
            close = Decimal(str(r["close"]))
            prev = Decimal(str(r["prev_close"]))
            out.append(Mover(
                symbol=str(r["symbol"]),
                prev_close=prev,
                close=close,
                change_abs=close - prev,
                pct_change=float(r["pct_change"]),
                rank=i,
            ))
        return out

    top = _to_movers(cm.nlargest(top_n, "pct_change"), ascending=False)
    bottom = _to_movers(cm.nsmallest(bottom_n, "pct_change"), ascending=True)

    if top and bottom:
        logger.info(
            f"market_movers: {final_date} — top1={top[0].symbol} "
            f"({top[0].pct_change:+.2f}%), bottom1={bottom[0].symbol} "
            f"({bottom[0].pct_change:+.2f}%)"
        )
    else:
        logger.info(f"market_movers: {final_date} — empty result")
    return MarketMovers(as_of_date=final_date, top=top, bottom=bottom)


def render_movers_block(movers: MarketMovers, *, instrument_symbol: str | None = None) -> str:
    """Render movers as a compact text block for the LLM prompt.

    If ``instrument_symbol`` matches one of the listed movers, append a
    one-line annotation calling that out — this is the per-candidate
    momentum tag described in the design.
    """
    if not movers.top and not movers.bottom:
        return "MARKET MOVERS: (no prior-session bhavcopy available)\n"

    lines: list[str] = []
    lines.append(f"YESTERDAY'S F&O LEADERS ({movers.as_of_date.isoformat()}):")
    for m in movers.top:
        lines.append(
            f"  {m.rank:>2}. {m.symbol:<12} {m.pct_change:+6.2f}%  "
            f"₹{float(m.prev_close):,.2f} → ₹{float(m.close):,.2f}"
        )
    if movers.bottom:
        lines.append("YESTERDAY'S F&O LAGGARDS:")
        for m in movers.bottom:
            lines.append(
                f"  {m.rank:>2}. {m.symbol:<12} {m.pct_change:+6.2f}%  "
                f"₹{float(m.prev_close):,.2f} → ₹{float(m.close):,.2f}"
            )

    if instrument_symbol:
        match = movers.by_symbol().get(instrument_symbol.upper().strip())
        if match is not None:
            board = "gainer" if match.pct_change >= 0 else "loser"
            lines.append(
                f"THIS INSTRUMENT YESTERDAY: {match.symbol} was rank "
                f"#{match.rank} {board} ({match.pct_change:+.2f}%)."
            )

    return "\n".join(lines) + "\n"
