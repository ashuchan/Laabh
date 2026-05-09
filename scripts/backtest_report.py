"""Consolidated quant-tuning analysis report for a backtest range.

Reads ``backtest_runs`` + ``backtest_trades`` for the given (portfolio,
start, end) window and emits a single markdown report covering:

  1. Run summary — per-day NAV, P&L, trade count.
  2. Distributional + risk metrics — Sharpe, deflated Sharpe (with
     n_trials = number of distinct arms tried), bootstrap CI, max
     drawdown, profit factor.
  3. Per-arm breakdown — pnl, win rate, profit factor, trade-Sharpe.
     This is the bandit-tuning view: which arms to keep tuning, which
     are losers.
  4. Posterior-vs-realized residual per arm — surfaces miscalibration
     (the bandit's expected mean vs actual P&L).

Usage:
    python -m scripts.backtest_report \\
        --start-date 2026-05-04 --end-date 2026-05-08 \\
        --portfolio-id <uuid> [--out reports/backtest_<dates>.md]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from src.db import session_scope
from src.models.backtest_run import BacktestRun
from src.models.backtest_trade import BacktestTrade
from src.quant.backtest.reporting.metrics import compute_metrics
from src.quant.backtest.reporting.per_arm import per_arm_stats


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _parse_uuid(s: str) -> uuid.UUID:
    try:
        return uuid.UUID(s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Not a valid UUID: {s!r}") from exc


def _f(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, Decimal):
        return float(v)
    return float(v)


def _fmt_pct(v: float, places: int = 4) -> str:
    return f"{v * 100:+.{places}f}%"


async def _load(portfolio_id: uuid.UUID, start: date, end: date):
    async with session_scope() as session:
        runs = (await session.execute(
            select(BacktestRun)
            .where(BacktestRun.portfolio_id == portfolio_id)
            .where(BacktestRun.backtest_date >= start)
            .where(BacktestRun.backtest_date <= end)
            .order_by(BacktestRun.backtest_date)
        )).scalars().all()
        if not runs:
            return [], []
        run_ids = [r.id for r in runs]
        trades = (await session.execute(
            select(BacktestTrade).where(BacktestTrade.backtest_run_id.in_(run_ids))
        )).scalars().all()
    return runs, trades


def _residual_by_arm(trades) -> dict[str, dict]:
    """Posterior-mean vs realized-pnl-per-trade residual per arm.

    Surfaces bandit miscalibration. A negative residual means the bandit's
    expected mean was higher than what arms actually delivered.
    """
    bucket: dict[str, list[tuple[float, float]]] = {}
    for t in trades:
        if t.realized_pnl is None:
            continue
        arm = t.arm_id
        post = _f(t.posterior_mean_at_entry)
        # Realized "return" per trade — normalize by entry premium so it's
        # on the same scale as posterior_mean (which is a return estimate).
        entry = _f(t.entry_premium_net)
        realized_ret = _f(t.realized_pnl) / entry if entry > 0 else 0.0
        bucket.setdefault(arm, []).append((post, realized_ret))
    out = {}
    for arm, pairs in bucket.items():
        n = len(pairs)
        avg_post = sum(p for p, _ in pairs) / n
        avg_real = sum(r for _, r in pairs) / n
        out[arm] = {
            "n": n,
            "avg_posterior": avg_post,
            "avg_realized": avg_real,
            "residual": avg_real - avg_post,
        }
    return out


def _build_report(portfolio_id: uuid.UUID, start: date, end: date, runs, trades) -> str:
    lines: list[str] = []
    lines.append(f"# Backtest analysis report")
    lines.append("")
    lines.append(f"- **Portfolio:** `{portfolio_id}`")
    lines.append(f"- **Range:** {start} → {end}")
    lines.append(f"- **Generated:** {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"- **Days replayed:** {len(runs)}")
    lines.append(f"- **Trades:** {len(trades)}")
    lines.append("")

    # Per-day summary
    lines.append("## 1. Per-day summary")
    lines.append("")
    lines.append("| Date | Start NAV | Final NAV | P&L % | Trades | Wins | Status |")
    lines.append("|---|---:|---:|---:|---:|---:|---|")
    daily_returns: list[float] = []
    for r in runs:
        snav = _f(r.starting_nav)
        fnav = _f(r.final_nav) if r.final_nav is not None else None
        pnl = _f(r.pnl_pct) if r.pnl_pct is not None else None
        if pnl is not None:
            daily_returns.append(pnl)
        status = "ok" if r.completed_at is not None else "incomplete"
        fnav_cell = f"{fnav:,.2f}" if fnav is not None else "—"
        pnl_cell = _fmt_pct(pnl) if pnl is not None else "—"
        lines.append(
            f"| {r.backtest_date} | {snav:,.2f} | {fnav_cell} | {pnl_cell} | "
            f"{r.trade_count or 0} | {r.winning_trades or 0} | {status} |"
        )
    # Cumulative
    nav = 1.0
    for r in daily_returns:
        nav *= 1.0 + r
    lines.append("")
    lines.append(f"**Cumulative (compounded) P&L:** {_fmt_pct(nav - 1.0)}")
    lines.append("")

    # Metrics
    lines.append("## 2. Distributional + risk metrics")
    lines.append("")
    if len(daily_returns) >= 2:
        # n_trials = number of distinct arms tested (proxy for multiple-testing inflation)
        n_arms = len({t.arm_id for t in trades}) or 1
        bundle = compute_metrics(
            daily_returns,
            n_trials=n_arms,
            bootstrap_iter=1000,
            bootstrap_block_size=min(5, max(1, len(daily_returns) // 2)),
            seed=42,
        )
        lines.append("| Metric | Value | Interpretation |")
        lines.append("|---|---:|---|")
        lines.append(f"| Days (n) | {bundle.n} | sample size |")
        lines.append(f"| Mean daily return | {_fmt_pct(bundle.mean)} | |")
        lines.append(f"| Median daily return | {_fmt_pct(bundle.median)} | |")
        lines.append(f"| Std (daily) | {_fmt_pct(bundle.std)} | |")
        lines.append(f"| Skew | {bundle.skew:+.4f} | normal=0; left-skewed = fat left tail |")
        lines.append(f"| Excess kurtosis | {bundle.kurtosis_excess:+.4f} | normal=0; >0 = fat tails |")
        lines.append(f"| Sharpe (annualised) | {bundle.sharpe:+.3f} | |")
        lines.append(
            f"| Deflated Sharpe (n_trials={n_arms}) | {bundle.deflated_sharpe:.4f} | "
            f"P(true Sharpe > 0); >0.95 significant, <0.5 worse than random |"
        )
        lines.append(
            f"| Sharpe 95% CI (block bootstrap) | "
            f"[{bundle.sharpe_ci_lower:+.3f}, {bundle.sharpe_ci_upper:+.3f}] | "
            f"contains 0 → not significantly non-zero |"
        )
        lines.append(f"| Win rate (per day) | {bundle.win_rate:.2%} | |")
        lines.append(f"| Avg win / day | {_fmt_pct(bundle.avg_win)} | |")
        lines.append(f"| Avg loss / day | {_fmt_pct(bundle.avg_loss)} | |")
        lines.append(f"| Profit factor | {bundle.profit_factor:.3f} | wins / losses; >1 profitable |")
        lines.append(f"| Max drawdown | {_fmt_pct(bundle.max_drawdown)} | peak-to-trough |")
        lines.append(f"| Calmar | {bundle.calmar:+.3f} | return / drawdown |")
        lines.append("")
        lines.append(
            "> **Caveat:** with n=" + str(bundle.n) + " days, all metrics here are "
            "noise-dominated. Treat this as a smoke-test of the harness, not as "
            "evidence of strategy edge. A meaningful walk-forward needs ≥3 months."
        )
    else:
        lines.append(f"_Not enough days ({len(daily_returns)}) to compute distributional metrics._")
    lines.append("")

    # Per-arm
    lines.append("## 3. Per-arm performance (bandit-tuning view)")
    lines.append("")
    arm_stats = per_arm_stats(trades)
    if arm_stats:
        lines.append(
            "| Arm | Trades | Total P&L | Win rate | Avg win | Avg loss | "
            "Profit factor | Avg hold (min) | Trade-Sharpe |"
        )
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for s in arm_stats:
            pf = "∞" if s.profit_factor == float("inf") else f"{s.profit_factor:.2f}"
            lines.append(
                f"| `{s.arm_id}` | {s.trade_count} | {s.pnl_total:+,.2f} | "
                f"{s.win_rate:.1%} | {s.avg_win:+,.2f} | {s.avg_loss:+,.2f} | "
                f"{pf} | {s.avg_holding_minutes:.1f} | {s.sharpe:+.3f} |"
            )
        lines.append("")
    else:
        lines.append("_No closed trades to analyse._")
    lines.append("")

    # Calibration residuals
    lines.append("## 4. Bandit calibration: posterior vs realized")
    lines.append("")
    resid = _residual_by_arm(trades)
    if resid:
        lines.append("| Arm | n | Avg posterior | Avg realized | Residual |")
        lines.append("|---|---:|---:|---:|---:|")
        for arm, d in sorted(resid.items(), key=lambda kv: kv[1]["residual"]):
            lines.append(
                f"| `{arm}` | {d['n']} | {d['avg_posterior']:+.4f} | "
                f"{d['avg_realized']:+.4f} | {d['residual']:+.4f} |"
            )
        lines.append("")
        lines.append(
            "> Negative residuals = bandit overestimated. Persistently large "
            "residuals across many trades signal a need to widen the prior or "
            "increase the bandit's exploration rate."
        )
    else:
        lines.append("_No closed trades to compute calibration residuals._")
    lines.append("")

    # Provenance + universe
    lines.append("## 5. Universe & provenance")
    lines.append("")
    if runs:
        last = runs[-1]
        lines.append(f"- **Bandit seed:** `{last.bandit_seed}` (same seed → bit-identical results)")
        lines.append(f"- **Git SHA at run:** `{last.git_sha or 'unknown'}`")
        univ = last.universe or []
        univ_syms = ", ".join(sorted({u.get('symbol', '?') for u in univ}))
        lines.append(f"- **Final-day universe ({len(univ)}):** {univ_syms}")
        # Source mix
        chain_sources: dict[str, int] = {}
        underlying_sources: dict[str, int] = {}
        for t in trades:
            chain_sources[t.chain_source or "unknown"] = chain_sources.get(t.chain_source or "unknown", 0) + 1
            underlying_sources[t.underlying_source or "unknown"] = underlying_sources.get(
                t.underlying_source or "unknown", 0
            ) + 1
        if chain_sources:
            lines.append(
                f"- **Chain provenance (trade count by source):** "
                + ", ".join(f"`{k}`: {v}" for k, v in sorted(chain_sources.items()))
            )
        if underlying_sources:
            lines.append(
                f"- **Underlying provenance:** "
                + ", ".join(f"`{k}`: {v}" for k, v in sorted(underlying_sources.items()))
            )
    lines.append("")

    return "\n".join(lines)


async def main_async(args: argparse.Namespace) -> int:
    runs, trades = await _load(args.portfolio_id, args.start_date, args.end_date)
    if not runs:
        print(
            f"No backtest_runs found for portfolio {args.portfolio_id} in "
            f"{args.start_date}..{args.end_date}. Run `python -m scripts.backtest_run` first.",
            file=sys.stderr,
        )
        return 1
    md = _build_report(args.portfolio_id, args.start_date, args.end_date, runs, trades)
    out_path = args.out or Path(
        f"reports/backtest_{args.start_date}_{args.end_date}.md"
    )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Python 3.7+
    except (AttributeError, OSError):
        pass
    try:
        print(md)
    except UnicodeEncodeError:
        # Windows console fallback: ASCII-only echo. Full content is in the file.
        print(md.encode("ascii", "replace").decode("ascii"))
    print()
    print(f"Report written to: {out_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="backtest_report",
        description="Generate a consolidated markdown report for a backtest range.",
    )
    p.add_argument("--start-date", type=_parse_date, required=True)
    p.add_argument("--end-date", type=_parse_date, required=True)
    p.add_argument("--portfolio-id", type=_parse_uuid, required=True)
    p.add_argument("--out", type=Path, default=None, help="Output markdown path")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
