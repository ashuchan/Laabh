"""Selection-funnel / missed-trades analysis for a backtest day.

Diagnoses *why* the quant algo didn't pick the day's actual top movers. For
each top-N intraday gainer we walk the rejection ladder:

  1. Not in universe         — symbol absent from ``backtest_runs.universe``.
                               Universe selector is the bottleneck.
  2. In universe, no signal  — symbol present but no row in
                               ``backtest_signal_log``. Primitives never
                               fired on this stock during the session.
  3. Signal too weak         — primitive emitted a signal but it failed the
                               ``laabh_quant_min_signal_strength`` gate.
  4. Lost the bandit draw    — passed every gate but a different arm was
                               selected at every tick where this one fired.
  5. Sized to zero           — bandit picked it, but ``compute_lots`` returned
                               0 (Kelly fraction or exposure cap denied entry).

Plus traded: opened a position; we then show the realized P&L versus the
theoretical maximum move (open → high) for context.

Usage:
    python -m scripts.missed_trades \\
        --date 2026-05-08 --portfolio-id <uuid> [--top-n 10]

Reads ``price_intraday``, ``backtest_runs``, ``backtest_signal_log``,
``backtest_trades``. Read-only.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytz
from sqlalchemy import and_, func, select

from src.db import session_scope
from src.models.backtest_run import BacktestRun
from src.models.backtest_signal_log import BacktestSignalLog
from src.models.backtest_trade import BacktestTrade
from src.models.instrument import Instrument
from src.models.price_intraday import PriceIntraday


_IST = pytz.timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class GainerRow:
    """One row of the top-gainers table (intraday move on the trading day)."""

    instrument_id: uuid.UUID
    symbol: str
    name: str | None
    open_px: float
    high_px: float
    close_px: float
    max_move_pct: float       # (high - open) / open  — best possible long
    close_move_pct: float     # (close - open) / open — what a buy-and-hold gets


@dataclass
class FunnelRow:
    """One row of the funnel report — a gainer plus its rejection bucket."""

    gainer: GainerRow
    in_universe: bool
    bucket: str               # 'not_in_universe' | 'no_signal' | 'weak_signal'
                              # | 'lost_bandit'    | 'sized_zero' | 'opened'
    signal_count: int = 0
    reason_breakdown: dict[str, int] = field(default_factory=dict)
    best_strength: float | None = None
    best_posterior: float | None = None
    trade_pnl_pct: float | None = None       # realized P&L / entry premium
    trade_arm_id: str | None = None
    trade_lots: int | None = None


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _utc_session_window(trading_date: date) -> tuple[datetime, datetime]:
    """Return (session_open_utc, session_close_utc) bracketing the IST trading day."""
    open_ist = _IST.localize(datetime.combine(trading_date, time(9, 15)))
    close_ist = _IST.localize(datetime.combine(trading_date, time(15, 30)))
    return open_ist.astimezone(timezone.utc), close_ist.astimezone(timezone.utc)


async def _load_run(
    session, portfolio_id: uuid.UUID, trading_date: date
) -> BacktestRun | None:
    q = (
        select(BacktestRun)
        .where(BacktestRun.portfolio_id == portfolio_id)
        .where(BacktestRun.backtest_date == trading_date)
        .order_by(BacktestRun.started_at.desc())
        .limit(1)
    )
    return (await session.execute(q)).scalar_one_or_none()


async def _load_top_gainers(
    session, trading_date: date, *, top_n: int
) -> list[GainerRow]:
    """Top-N intraday gainers among F&O instruments for the date.

    Ranked by (max(high) - first(open)) / first(open) — the largest *possible*
    long move, which is what a working primitive ought to catch. Stocks with
    no intraday bars on the date are silently excluded.
    """
    open_utc, close_utc = _utc_session_window(trading_date)

    q = (
        select(
            Instrument.id,
            Instrument.symbol,
            Instrument.company_name.label("name"),
            func.min(PriceIntraday.timestamp).label("first_ts"),
            func.max(PriceIntraday.high).label("max_high"),
            func.max(PriceIntraday.timestamp).label("last_ts"),
        )
        .join(PriceIntraday, PriceIntraday.instrument_id == Instrument.id)
        .where(
            and_(
                Instrument.is_fno.is_(True),
                Instrument.is_active.is_(True),
                PriceIntraday.timestamp >= open_utc,
                PriceIntraday.timestamp <= close_utc,
            )
        )
        .group_by(Instrument.id, Instrument.symbol, Instrument.company_name)
    )
    rows = (await session.execute(q)).all()
    if not rows:
        return []

    # We need the open at first_ts and close at last_ts for each instrument.
    # Two indexed lookups per row keep us under N+1 only loosely (small N).
    out: list[GainerRow] = []
    for r in rows:
        first_q = (
            select(PriceIntraday.open).where(
                and_(
                    PriceIntraday.instrument_id == r.id,
                    PriceIntraday.timestamp == r.first_ts,
                )
            )
        )
        last_q = (
            select(PriceIntraday.close).where(
                and_(
                    PriceIntraday.instrument_id == r.id,
                    PriceIntraday.timestamp == r.last_ts,
                )
            )
        )
        open_px = (await session.execute(first_q)).scalar()
        close_px = (await session.execute(last_q)).scalar()
        if open_px is None or close_px is None or float(open_px) == 0:
            continue
        op = float(open_px)
        hi = float(r.max_high)
        cl = float(close_px)
        out.append(
            GainerRow(
                instrument_id=r.id,
                symbol=r.symbol,
                name=r.name,
                open_px=op,
                high_px=hi,
                close_px=cl,
                max_move_pct=(hi - op) / op,
                close_move_pct=(cl - op) / op,
            )
        )
    out.sort(key=lambda g: g.max_move_pct, reverse=True)
    return out[:top_n]


async def _load_signal_logs_by_symbol(
    session, run_id: uuid.UUID
) -> dict[str, list[BacktestSignalLog]]:
    q = select(BacktestSignalLog).where(BacktestSignalLog.backtest_run_id == run_id)
    rows = list((await session.execute(q)).scalars())
    out: dict[str, list[BacktestSignalLog]] = {}
    for r in rows:
        out.setdefault(r.symbol, []).append(r)
    return out


async def _load_trades_by_underlying(
    session, run_id: uuid.UUID
) -> dict[uuid.UUID, list[BacktestTrade]]:
    q = select(BacktestTrade).where(BacktestTrade.backtest_run_id == run_id)
    rows = list((await session.execute(q)).scalars())
    out: dict[uuid.UUID, list[BacktestTrade]] = {}
    for t in rows:
        out.setdefault(t.underlying_id, []).append(t)
    return out


# ---------------------------------------------------------------------------
# Bucket classification
# ---------------------------------------------------------------------------

# Order matters: when a gainer has multiple signal-log rows, we surface the
# *worst* (latest in this list) bucket, since that's the one that actually
# blocked entry on the most-promising tick. Earlier reasons are upstream
# rejections that subsume later ones.
_BUCKET_PRIORITY = [
    "weak_signal",     # bucket 3
    "warmup",
    "kill_switch",
    "capacity_full",
    "cooloff",
    "lost_bandit",     # bucket 4
    "sized_zero",      # bucket 5
    "opened",
]


def _dominant_bucket(reason_counts: dict[str, int]) -> str:
    """Pick the most-actionable bucket from a tick-disposition tally.

    "Most-actionable" = the latest-stage rejection that fired, since that's
    where tuning effort pays off. ``opened`` always wins if present.
    """
    if reason_counts.get("opened", 0) > 0:
        return "opened"
    # Walk from latest stage backwards — we want the deepest stage reached.
    for reason in reversed(_BUCKET_PRIORITY[:-1]):
        if reason_counts.get(reason, 0) > 0:
            return reason
    return "no_signal"  # caller should have caught this; defensive fallback


def _classify(
    *,
    gainer: GainerRow,
    universe_symbols: set[str],
    signal_logs: list[BacktestSignalLog],
    trades: list[BacktestTrade],
) -> FunnelRow:
    if gainer.symbol not in universe_symbols:
        return FunnelRow(gainer=gainer, in_universe=False, bucket="not_in_universe")

    if not signal_logs:
        return FunnelRow(gainer=gainer, in_universe=True, bucket="no_signal")

    counts = Counter(r.rejection_reason for r in signal_logs)
    bucket = _dominant_bucket(counts)
    best_strength = max(abs(float(r.strength)) for r in signal_logs)
    posteriors = [
        float(r.posterior_mean) for r in signal_logs if r.posterior_mean is not None
    ]
    best_posterior = max(posteriors) if posteriors else None

    pnl_pct = None
    arm_id = None
    lots = None
    if trades:
        # Most recent trade wins for display
        t = sorted(trades, key=lambda x: x.entry_at)[-1]
        if t.realized_pnl is not None and t.entry_premium_net:
            pnl_pct = float(t.realized_pnl) / (float(t.entry_premium_net) * t.lots)
        arm_id = t.arm_id
        lots = t.lots

    return FunnelRow(
        gainer=gainer,
        in_universe=True,
        bucket=bucket,
        signal_count=len(signal_logs),
        reason_breakdown=dict(counts),
        best_strength=best_strength,
        best_posterior=best_posterior,
        trade_pnl_pct=pnl_pct,
        trade_arm_id=arm_id,
        trade_lots=lots,
    )


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

_BUCKET_LABEL = {
    "not_in_universe": "1. Not in universe",
    "no_signal":       "2. In universe, no signal",
    "weak_signal":     "3. Signal too weak",
    "warmup":          "Tick gated by warmup",
    "kill_switch":     "Tick gated by kill switch",
    "capacity_full":   "Tick gated by capacity",
    "cooloff":         "Arm in cooloff",
    "lost_bandit":     "4. Lost the bandit draw",
    "sized_zero":      "5. Sized to zero",
    "opened":          "Traded",
}


def _fmt_pct(v: float | None, places: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v * 100:+.{places}f}%"


def _build_report(
    *,
    portfolio_id: uuid.UUID,
    trading_date: date,
    run: BacktestRun,
    funnel: list[FunnelRow],
    universe_size: int,
) -> str:
    lines: list[str] = []
    lines.append(f"# Missed-trades funnel — {trading_date}")
    lines.append("")
    lines.append(f"- **Portfolio:** `{portfolio_id}`")
    lines.append(f"- **Backtest run:** `{run.id}`")
    lines.append(f"- **Universe size:** {universe_size}")
    lines.append(f"- **Generated:** {datetime.now().isoformat(timespec='seconds')}")
    lines.append("")

    # ---- Top gainers table ----
    lines.append("## Top intraday gainers — funnel disposition")
    lines.append("")
    lines.append(
        "| # | Symbol | Open→High | Open→Close | Bucket | Signals | Best strength | Trade P&L |"
    )
    lines.append("|---:|---|---:|---:|---|---:|---:|---:|")
    for i, row in enumerate(funnel, 1):
        g = row.gainer
        lines.append(
            f"| {i} | `{g.symbol}` | {_fmt_pct(g.max_move_pct)} | "
            f"{_fmt_pct(g.close_move_pct)} | {_BUCKET_LABEL.get(row.bucket, row.bucket)} | "
            f"{row.signal_count} | "
            f"{f'{row.best_strength:.3f}' if row.best_strength is not None else '—'} | "
            f"{_fmt_pct(row.trade_pnl_pct)} |"
        )
    lines.append("")

    # ---- Funnel summary ----
    counts: Counter[str] = Counter(r.bucket for r in funnel)
    lines.append("## Funnel summary")
    lines.append("")
    lines.append("| Bucket | Count | % of top gainers |")
    lines.append("|---|---:|---:|")
    n = max(1, len(funnel))
    # Order: ladder from cheapest fix to deepest pipeline reach
    order = [
        "not_in_universe", "no_signal", "weak_signal",
        "warmup", "kill_switch", "capacity_full", "cooloff",
        "lost_bandit", "sized_zero", "opened",
    ]
    for b in order:
        c = counts.get(b, 0)
        if c == 0:
            continue
        lines.append(f"| {_BUCKET_LABEL.get(b, b)} | {c} | {c / n:.1%} |")
    lines.append("")

    # ---- Per-bucket detail ----
    by_bucket: dict[str, list[FunnelRow]] = {}
    for r in funnel:
        by_bucket.setdefault(r.bucket, []).append(r)

    lines.append("## Per-bucket detail")
    lines.append("")

    for b in order:
        rows = by_bucket.get(b, [])
        if not rows:
            continue
        lines.append(f"### {_BUCKET_LABEL.get(b, b)} — {len(rows)} symbol(s)")
        lines.append("")
        if b == "not_in_universe":
            lines.append(
                "_The selector picks tomorrow's universe from yesterday's "
                "data — fresh rippers are invisible by construction. Below "
                "are the symbols missed because they weren't on yesterday's "
                "leaderboard._"
            )
            lines.append("")
            lines.append("| Symbol | Open→High | Open→Close |")
            lines.append("|---|---:|---:|")
            for r in rows:
                lines.append(
                    f"| `{r.gainer.symbol}` | {_fmt_pct(r.gainer.max_move_pct)} | "
                    f"{_fmt_pct(r.gainer.close_move_pct)} |"
                )
        elif b == "no_signal":
            lines.append(
                "_Symbol was in the universe but no primitive ever emitted a "
                "non-None signal during the session. Either warmup wasn't "
                "satisfied or the primitives' setup conditions weren't met._"
            )
            lines.append("")
            lines.append("| Symbol | Open→High | Open→Close |")
            lines.append("|---|---:|---:|")
            for r in rows:
                lines.append(
                    f"| `{r.gainer.symbol}` | {_fmt_pct(r.gainer.max_move_pct)} | "
                    f"{_fmt_pct(r.gainer.close_move_pct)} |"
                )
        elif b == "weak_signal":
            lines.append(
                "_Primitive(s) fired but every signal was below "
                "`laabh_quant_min_signal_strength`. Either the gate is too "
                "high or the primitives need tuning._"
            )
            lines.append("")
            lines.append("| Symbol | Open→High | Best strength | Tick count |")
            lines.append("|---|---:|---:|---:|")
            for r in rows:
                lines.append(
                    f"| `{r.gainer.symbol}` | {_fmt_pct(r.gainer.max_move_pct)} | "
                    f"{r.best_strength:.3f} | {r.signal_count} |"
                )
        elif b == "lost_bandit":
            lines.append(
                "_Signal passed the strength gate, the arm wasn't in cooloff, "
                "but at every tick the bandit picked a different arm. "
                "Expected when n_obs is small (LinTS exploration) — but "
                "consistent losses across many days suggest stale priors._"
            )
            lines.append("")
            lines.append(
                "| Symbol | Open→High | Best strength | Best posterior | Tick count |"
            )
            lines.append("|---|---:|---:|---:|---:|")
            for r in rows:
                lines.append(
                    f"| `{r.gainer.symbol}` | {_fmt_pct(r.gainer.max_move_pct)} | "
                    f"{r.best_strength:.3f} | "
                    f"{f'{r.best_posterior:+.4f}' if r.best_posterior is not None else '—'} | "
                    f"{r.signal_count} |"
                )
        elif b == "sized_zero":
            lines.append(
                "_Bandit picked these arms but `compute_lots` returned 0. "
                "Usually the cost gate (gross < 1.5 × estimated costs) or the "
                "exposure cap. Look at the sizer params if this is dominant._"
            )
            lines.append("")
            lines.append(
                "| Symbol | Open→High | Best strength | Best posterior | Tick count |"
            )
            lines.append("|---|---:|---:|---:|---:|")
            for r in rows:
                lines.append(
                    f"| `{r.gainer.symbol}` | {_fmt_pct(r.gainer.max_move_pct)} | "
                    f"{r.best_strength:.3f} | "
                    f"{f'{r.best_posterior:+.4f}' if r.best_posterior is not None else '—'} | "
                    f"{r.signal_count} |"
                )
        elif b == "opened":
            lines.append(
                "_Trade was actually placed. Compare realized P&L vs the "
                "theoretical max (open→high) to see how much of the move was "
                "captured. Big gaps point at exit timing, not entry._"
            )
            lines.append("")
            lines.append(
                "| Symbol | Arm | Lots | Open→High | Realized P&L per lot | Capture |"
            )
            lines.append("|---|---|---:|---:|---:|---:|")
            for r in rows:
                cap = (
                    r.trade_pnl_pct / r.gainer.max_move_pct
                    if r.trade_pnl_pct is not None
                    and r.gainer.max_move_pct
                    else None
                )
                lines.append(
                    f"| `{r.gainer.symbol}` | `{r.trade_arm_id or '—'}` | "
                    f"{r.trade_lots if r.trade_lots is not None else '—'} | "
                    f"{_fmt_pct(r.gainer.max_move_pct)} | "
                    f"{_fmt_pct(r.trade_pnl_pct)} | {_fmt_pct(cap)} |"
                )
        else:
            # Tick-level gates — fall back to a plain list
            lines.append("| Symbol | Open→High | Tick count |")
            lines.append("|---|---:|---:|")
            for r in rows:
                lines.append(
                    f"| `{r.gainer.symbol}` | {_fmt_pct(r.gainer.max_move_pct)} | "
                    f"{r.signal_count} |"
                )
        lines.append("")

    # ---- Tick-level rejection breakdown (across all signals on the day) ----
    lines.append("## All-signals rejection breakdown")
    lines.append("")
    lines.append(
        "_Aggregates every row in ``backtest_signal_log`` for this run, not "
        "just the top gainers. Use this to spot dominant tick-level gates "
        "(e.g. capacity_full or kill_switch dominating means we're never "
        "even getting to the bandit)._"
    )
    lines.append("")
    return "\n".join(lines)


async def _all_signal_breakdown(session, run_id: uuid.UUID) -> dict[str, int]:
    q = (
        select(BacktestSignalLog.rejection_reason, func.count())
        .where(BacktestSignalLog.backtest_run_id == run_id)
        .group_by(BacktestSignalLog.rejection_reason)
    )
    return {row[0]: int(row[1]) for row in (await session.execute(q)).all()}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main_async(args: argparse.Namespace) -> int:
    async with session_scope() as session:
        run = await _load_run(session, args.portfolio_id, args.date)
        if run is None:
            print(
                f"No backtest_run found for portfolio {args.portfolio_id} on "
                f"{args.date}. Run `python -m scripts.backtest_run` first.",
                file=sys.stderr,
            )
            return 1
        gainers = await _load_top_gainers(session, args.date, top_n=args.top_n)
        if not gainers:
            print(
                f"No price_intraday data for {args.date} — cannot compute "
                f"top gainers. Backfill the day first.",
                file=sys.stderr,
            )
            return 1
        signal_logs_by_symbol = await _load_signal_logs_by_symbol(session, run.id)
        trades_by_underlying = await _load_trades_by_underlying(session, run.id)
        all_breakdown = await _all_signal_breakdown(session, run.id)

    universe_symbols: set[str] = {
        u.get("symbol") for u in (run.universe or []) if u.get("symbol")
    }

    funnel = [
        _classify(
            gainer=g,
            universe_symbols=universe_symbols,
            signal_logs=signal_logs_by_symbol.get(g.symbol, []),
            trades=trades_by_underlying.get(g.instrument_id, []),
        )
        for g in gainers
    ]

    md = _build_report(
        portfolio_id=args.portfolio_id,
        trading_date=args.date,
        run=run,
        funnel=funnel,
        universe_size=len(universe_symbols),
    )
    if all_breakdown:
        md += "| Reason | Count |\n|---|---:|\n"
        for k in sorted(all_breakdown, key=lambda k: -all_breakdown[k]):
            md += f"| {_BUCKET_LABEL.get(k, k)} | {all_breakdown[k]} |\n"
    else:
        md += "_No signal-log rows for this run — was the run replayed before "
        md += "the funnel-log instrumentation was added? Re-run the backtest._\n"

    out_path = args.out or Path(f"reports/missed_trades_{args.date}.md")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    try:
        print(md)
    except UnicodeEncodeError:
        print(md.encode("ascii", "replace").decode("ascii"))
    print()
    print(f"Report written to: {out_path}")
    return 0


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _parse_uuid(s: str) -> uuid.UUID:
    try:
        return uuid.UUID(s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Not a valid UUID: {s!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="missed_trades",
        description="Selection-funnel report: why didn't the algo pick the day's top gainers?",
    )
    p.add_argument("--date", type=_parse_date, required=True, help="YYYY-MM-DD")
    p.add_argument("--portfolio-id", type=_parse_uuid, required=True)
    p.add_argument("--top-n", type=int, default=10, help="How many gainers to analyse")
    p.add_argument("--out", type=Path, default=None, help="Output markdown path")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
