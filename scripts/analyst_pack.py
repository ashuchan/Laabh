"""Analyst pack — single-markdown summary of a backtest range, designed for
ingestion by Claude Desktop (or any other LLM analyst).

Outputs two files:
  * ``reports/analyst_pack_<start>_<end>.md`` — the headline doc, dense
    enough that an LLM has full context but small enough (~50 KB) to
    paste into one chat.
  * ``reports/trades_<start>_<end>.csv``      — sidecar with one row per
    trade for follow-up crunching.

Sections of the markdown (in this order):
  1. TL;DR — one paragraph + per-day P&L sparkline-style table.
  2. Per-arm leaderboard — what's working, what isn't.
  3. Funnel breakdown — where signals die in the pipeline.
  4. Sample trade traces — top winners + worst losers, each rendered with
     the FeatureBundle, primitive formula, bandit tournament, sizer cascade.
  5. Sample missed top-gainers — what we should have caught.
  6. Configuration snapshot.
  7. Suggested questions to ask Claude.

Usage:
    python -m scripts.analyst_pack \\
        --start-date 2026-05-04 --end-date 2026-05-08 \\
        --portfolio-id <uuid>
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import sys
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from src.db import session_scope
from src.models.instrument import Instrument
from src.quant.inspector import (
    SessionSkeleton,
    TickState,
    TradeRecord,
    list_runs,
    load_session_skeleton,
    load_tick_state,
)

# Reused from missed_trades.py — avoids duplicating the gainer/classification
# logic. These are private names but stable; refactor into a shared module
# only when a third consumer appears.
from scripts.missed_trades import (
    FunnelRow,
    GainerRow,
    _classify,
    _load_top_gainers,
    _load_signal_logs_by_symbol,
    _load_trades_by_underlying,
)


# ---------------------------------------------------------------------------
# Pure helpers (no DB / no I/O) — unit-testable in isolation.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArmStats:
    """Aggregated stats for one arm across the range."""

    arm_id: str
    primitive_name: str
    n_trades: int
    n_wins: int
    total_pnl: float
    avg_pnl_per_trade: float
    win_rate: float
    profit_factor: float          # gross_wins / |gross_losses|
    avg_holding_minutes: float


def _compute_arm_stats(trades: list[TradeRecord]) -> list[ArmStats]:
    """One ArmStats row per distinct ``arm_id``, sorted by total_pnl desc.

    Open trades (no exit / no realized_pnl) are skipped from the aggregates
    so a single open position doesn't poison the win-rate stat.
    """
    by_arm: dict[str, list[TradeRecord]] = defaultdict(list)
    for t in trades:
        if t.realized_pnl is None:
            continue
        by_arm[t.arm_id].append(t)
    out: list[ArmStats] = []
    for arm_id, arm_trades in by_arm.items():
        n = len(arm_trades)
        pnls = [float(t.realized_pnl) for t in arm_trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        total = sum(pnls)
        gross_w = sum(wins) or 0.0
        gross_l = abs(sum(losses)) or 1e-9  # avoid div0; ~∞ in display
        hold_min = []
        for t in arm_trades:
            if t.exit_at is None:
                continue
            hold_min.append((t.exit_at - t.entry_at).total_seconds() / 60.0)
        out.append(
            ArmStats(
                arm_id=arm_id,
                primitive_name=arm_trades[0].primitive_name,
                n_trades=n,
                n_wins=len(wins),
                total_pnl=total,
                avg_pnl_per_trade=total / n if n else 0.0,
                win_rate=len(wins) / n if n else 0.0,
                profit_factor=gross_w / gross_l,
                avg_holding_minutes=(sum(hold_min) / len(hold_min)) if hold_min else 0.0,
            )
        )
    out.sort(key=lambda s: s.total_pnl, reverse=True)
    return out


def _pick_trace_samples(
    trades: list[TradeRecord], *, k_winners: int = 2, k_losers: int = 2,
) -> list[TradeRecord]:
    """Pick the top-K winners + worst-K losers for trace rendering.

    Closed trades only (need realized_pnl to rank). Deduplicated so a trade
    that's both top-K winner and isn't double-counted (impossible by P&L
    sign, but defensive).
    """
    closed = [t for t in trades if t.realized_pnl is not None]
    if not closed:
        return []
    by_pnl_desc = sorted(closed, key=lambda t: float(t.realized_pnl), reverse=True)
    winners = by_pnl_desc[:k_winners]
    losers = by_pnl_desc[-k_losers:] if k_losers > 0 else []
    seen = set()
    picks: list[TradeRecord] = []
    for t in winners + losers:
        if t.trade_id in seen:
            continue
        seen.add(t.trade_id)
        picks.append(t)
    return picks


def _aggregate_funnel_buckets(skeletons: list[SessionSkeleton]) -> Counter:
    """Sum every TickSummary's per-bucket count across all skeletons.

    Returns a Counter keyed by bucket name. Useful for the cross-range
    funnel insight ("60% of strong signals lost the bandit draw → consider
    forget factor / priors").
    """
    c: Counter = Counter()
    for skel in skeletons:
        for tick in skel.ticks:
            c["opened"]        += tick.n_opened
            c["lost_bandit"]   += tick.n_lost_bandit
            c["sized_zero"]    += tick.n_sized_zero
            c["cooloff"]       += tick.n_cooloff
            c["kill_switch"]   += tick.n_kill_switch
            c["capacity_full"] += tick.n_capacity_full
            c["warmup"]        += tick.n_warmup
            c["weak_signal"]   += (tick.n_signals_total - tick.n_signals_strong)
    return c


def _trades_csv_text(
    trades_with_meta: list[tuple[SessionSkeleton, TradeRecord, str]],
) -> str:
    """Build the sidecar CSV — one row per trade. Strings escaped properly."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "run_date", "run_id", "trade_id", "arm_id", "primitive", "symbol",
        "direction", "entry_at_utc", "exit_at_utc", "hold_minutes",
        "lots", "entry_premium_net", "exit_premium_net", "realized_pnl",
        "pnl_per_lot_pct", "exit_reason",
    ])
    for skel, t, symbol in trades_with_meta:
        hold_min = (
            (t.exit_at - t.entry_at).total_seconds() / 60.0
            if t.exit_at else ""
        )
        pnl_per_lot_pct = (
            (t.realized_pnl / (t.entry_premium_net * t.lots))
            if (t.realized_pnl is not None and t.entry_premium_net and t.lots) else ""
        )
        w.writerow([
            skel.metadata.backtest_date.isoformat(),
            str(skel.metadata.run_id),
            str(t.trade_id),
            t.arm_id,
            t.primitive_name,
            symbol,
            t.direction,
            t.entry_at.astimezone(timezone.utc).isoformat(),
            t.exit_at.astimezone(timezone.utc).isoformat() if t.exit_at else "",
            f"{hold_min:.2f}" if hold_min != "" else "",
            t.lots,
            f"{t.entry_premium_net:.2f}",
            f"{t.exit_premium_net:.2f}" if t.exit_premium_net is not None else "",
            f"{t.realized_pnl:.2f}" if t.realized_pnl is not None else "",
            f"{pnl_per_lot_pct:.6f}" if pnl_per_lot_pct != "" else "",
            t.exit_reason or "",
        ])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Markdown rendering helpers
# ---------------------------------------------------------------------------

def _fmt_pct(v: float | None, places: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v * 100:+.{places}f}%"


def _fmt_money(v: float | None) -> str:
    if v is None:
        return "—"
    return f"₹{v:+,.0f}"


def _fmt_pf(pf: float) -> str:
    """Profit factor — show ∞ when losses are essentially zero."""
    if pf > 1e6:
        return "∞"
    return f"{pf:.2f}"


def _render_kv_block(d: dict | None) -> str:
    """Render a JSONB sub-dict (inputs / intermediates) as a compact list."""
    if not d:
        return "_(empty)_"
    return "\n".join(f"- `{k}`: {v}" for k, v in d.items())


def _render_trade_trace(
    *, skel: SessionSkeleton, trade: TradeRecord, focused: TickState, full: TickState,
    symbol: str,
) -> str:
    """Render one trade's full entry-tick trace as a markdown section."""
    lines: list[str] = []
    pnl_str = _fmt_money(trade.realized_pnl)
    pnl_pct_str = (
        _fmt_pct(trade.realized_pnl / (trade.entry_premium_net * trade.lots))
        if trade.realized_pnl is not None
        and trade.entry_premium_net and trade.lots else "—"
    )
    hold_min = (
        (trade.exit_at - trade.entry_at).total_seconds() / 60.0
        if trade.exit_at else None
    )
    headline = (
        f"### `{trade.arm_id}` × {trade.lots} lots  ·  "
        f"{trade.direction}  ·  P&L {pnl_str} ({pnl_pct_str})"
    )
    lines.append(headline)
    lines.append("")
    lines.append(
        f"- **Symbol**: `{symbol}` ({skel.metadata.backtest_date})\n"
        f"- **Entry**: {trade.entry_at.strftime('%H:%M:%S UTC')} @ ₹{trade.entry_premium_net:,.2f}/lot\n"
        f"- **Exit**:  {trade.exit_at.strftime('%H:%M:%S UTC') if trade.exit_at else '—'}"
        f" @ ₹{trade.exit_premium_net:,.2f}/lot" if trade.exit_premium_net else ""
    )
    if hold_min is not None:
        lines.append(f"- **Held**: {hold_min:.1f} min  ·  **Exit reason**: `{trade.exit_reason or '—'}`")

    # The primitive's formula at entry — the "why we got in"
    own_signal = next(
        (s for s in focused.primitive_signals if s.arm_id == trade.arm_id),
        None,
    )
    lines.append("")
    lines.append("**Primitive at entry**")
    if own_signal and own_signal.primitive_trace:
        pt = own_signal.primitive_trace
        lines.append(f"- name: `{pt.get('name')}`  ·  strength: `{own_signal.strength:+.4f}`")
        lines.append(f"- formula: `{pt.get('formula', '—')}`")
        if pt.get("intermediates"):
            lines.append("- intermediates:")
            for k, v in (pt.get("intermediates") or {}).items():
                lines.append(f"  - `{k}`: {v}")
    else:
        lines.append("_(no primitive trace recorded — likely a pre-PR-1 trade)_")

    # Bandit context — the "why this arm beat the others"
    lines.append("")
    lines.append("**Bandit tournament at entry**")
    if full.bandit_tournament:
        bt = full.bandit_tournament
        lines.append(f"- algo: `{bt.algo}`  ·  competitors: {bt.n_competitors}")
        if bt.context_vector and bt.context_dims:
            ctx_inline = ", ".join(
                f"{n}={v:.3f}" for n, v in zip(bt.context_dims, bt.context_vector)
            )
            lines.append(f"- context: `{ctx_inline}`")
        # Top 3 competing arms by score
        ranked = sorted(
            bt.arms.items(), key=lambda kv: float(kv[1].get("score", 0.0)), reverse=True,
        )[:3]
        lines.append("- top 3 by score:")
        for arm_id, payload in ranked:
            chosen = " ✓" if arm_id == bt.selected_arm_id else ""
            lines.append(
                f"  - `{arm_id}`{chosen}  "
                f"sampled={float(payload.get('sampled_mean', 0)):+.4f}  "
                f"strength={float(payload.get('signal_strength', 0)):.3f}  "
                f"score={float(payload.get('score', 0)):+.4f}"
            )
    else:
        lines.append("_(bandit didn't produce a tournament at this tick)_")

    # Sizer cascade — the "why this many lots"
    lines.append("")
    lines.append("**Sizer cascade**")
    if full.sizer_outcome:
        sz = full.sizer_outcome
        block_str = f"BLOCKED at `{sz.blocking_step}`" if sz.blocking_step else "no block"
        lines.append(f"- final lots: **{sz.final_lots}** ({block_str})")
        for step in sz.cascade:
            lines.append(
                f"  - `{step.get('step')}` → {step.get('value')}  "
                f"_({step.get('formula', '')})_"
            )
    else:
        lines.append("_(no sizer trace — pre-PR-1 trade)_")

    return "\n".join(lines)


def _build_markdown(
    *,
    portfolio_id: uuid.UUID,
    start: date,
    end: date,
    skeletons: list[SessionSkeleton],
    all_trades_with_meta: list[tuple[SessionSkeleton, TradeRecord, str]],
    arm_stats: list[ArmStats],
    funnel_counts: Counter,
    trace_renderings: list[str],
    missed_funnels: list[FunnelRow],
) -> str:
    L: list[str] = []

    # ---- Header ----
    L.append(f"# Analyst pack — {start} → {end}")
    L.append("")
    L.append(f"- **Portfolio**: `{portfolio_id}`")
    L.append(f"- **Generated**: {datetime.now().isoformat(timespec='seconds')}")
    L.append(f"- **Days replayed**: {len(skeletons)}")
    L.append(f"- **Total closed trades**: {sum(1 for _, t, _ in all_trades_with_meta if t.realized_pnl is not None)}")
    total_pnl = sum(
        float(t.realized_pnl) for _, t, _ in all_trades_with_meta
        if t.realized_pnl is not None
    )
    L.append(f"- **Net P&L (realized)**: {_fmt_money(total_pnl)}")
    L.append("")

    # ---- 1. TL;DR ----
    L.append("## 1. TL;DR")
    L.append("")
    if skeletons:
        per_day_pct = []
        for s in skeletons:
            p = s.metadata.pnl_pct
            per_day_pct.append(_fmt_pct(p) if p is not None else "—")
        cumulative = 1.0
        for s in skeletons:
            if s.metadata.pnl_pct is not None:
                cumulative *= 1.0 + float(s.metadata.pnl_pct)
        L.append(
            f"Across **{len(skeletons)} day(s)** the strategy compounded to "
            f"**{_fmt_pct(cumulative - 1.0)}**. Per-day P&L: "
            + " · ".join(per_day_pct)
        )
        # Funnel headline
        total_signals = sum(funnel_counts.values())
        if total_signals:
            top_bucket, top_count = funnel_counts.most_common(1)[0]
            L.append(
                f"\nDominant funnel bucket: **`{top_bucket}`** "
                f"({top_count} / {total_signals} signals = "
                f"{top_count / total_signals * 100:.1f}%)."
            )
    L.append("")

    # ---- 2. Per-day summary ----
    L.append("## 2. Per-day summary")
    L.append("")
    L.append("| Date | Start NAV | Final NAV | P&L % | Trades | Lock-in | Kill |")
    L.append("|---|---:|---:|---:|---:|:---:|:---:|")
    for s in skeletons:
        md = s.metadata
        # Trades for THIS day
        n_trades = sum(1 for skel, _, _ in all_trades_with_meta if skel is s)
        final_nav_cell = f"{md.final_nav:,.0f}" if md.final_nav is not None else "—"
        L.append(
            f"| {md.backtest_date} | "
            f"{md.starting_nav:,.0f} | "
            f"{final_nav_cell} | "
            f"{_fmt_pct(md.pnl_pct)} | "
            f"{n_trades} | — | — |"
        )
    L.append("")

    # ---- 3. Per-arm leaderboard ----
    L.append("## 3. Per-arm leaderboard")
    L.append("")
    if arm_stats:
        L.append("| Arm | Primitive | Trades | Wins | Win rate | Total P&L | Avg P&L | PF | Hold (min) |")
        L.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
        for s in arm_stats:
            L.append(
                f"| `{s.arm_id}` | {s.primitive_name} | {s.n_trades} | {s.n_wins} | "
                f"{s.win_rate * 100:.0f}% | {_fmt_money(s.total_pnl)} | "
                f"{_fmt_money(s.avg_pnl_per_trade)} | {_fmt_pf(s.profit_factor)} | "
                f"{s.avg_holding_minutes:.1f} |"
            )
    else:
        L.append("_No closed trades to leaderboard._")
    L.append("")

    # ---- 4. Funnel breakdown ----
    L.append("## 4. Funnel breakdown — where signals die")
    L.append("")
    total_signals = sum(funnel_counts.values())
    if total_signals:
        L.append("| Bucket | Count | % |")
        L.append("|---|---:|---:|")
        for bucket, count in funnel_counts.most_common():
            L.append(f"| `{bucket}` | {count:,} | {count / total_signals * 100:.1f}% |")
        L.append("")
        L.append(
            "> **Reading the funnel:** A high `lost_bandit` % means the bandit "
            "is over-exploring weak arms; consider tightening priors or lowering "
            "the forget factor. A high `weak_signal` % means primitives are "
            "firing but failing the strength gate; consider lowering "
            "`laabh_quant_min_signal_strength` or recalibrating the primitives. "
            "A high `sized_zero` % means the cost gate or exposure cap is "
            "denying entries."
        )
    else:
        L.append("_No signal-log rows in this range — re-run after PR 1's funnel-log instrumentation._")
    L.append("")

    # ---- 5. Sample trade traces ----
    L.append("## 5. Sample trade traces (top winners + worst losers)")
    L.append("")
    if trace_renderings:
        for rendering in trace_renderings:
            L.append(rendering)
            L.append("")
            L.append("---")
            L.append("")
    else:
        L.append("_Not enough closed trades with traces to surface samples._")
    L.append("")

    # ---- 6. Sample missed top-gainers ----
    L.append("## 6. Sample missed top-gainers")
    L.append("")
    if missed_funnels:
        L.append("| Date | Symbol | Open→High | Open→Close | Bucket |")
        L.append("|---|---|---:|---:|---|")
        for r in missed_funnels:
            L.append(
                f"| (latest) | `{r.gainer.symbol}` | "
                f"{_fmt_pct(r.gainer.max_move_pct)} | "
                f"{_fmt_pct(r.gainer.close_move_pct)} | `{r.bucket}` |"
            )
    else:
        L.append("_No missed-gainer data — runs may pre-date the funnel-log._")
    L.append("")

    # ---- 7. Configuration snapshot ----
    L.append("## 7. Configuration snapshot (latest run)")
    L.append("")
    if skeletons:
        cfg = skeletons[-1].config_snapshot
        if cfg:
            L.append("```json")
            import json
            L.append(json.dumps(cfg, indent=2, sort_keys=True))
            L.append("```")
    L.append("")

    # ---- 8. Suggested questions for Claude ----
    L.append("## 8. Suggested prompts for Claude")
    L.append("")
    L.append(
        "1. *\"Look at section 3 (per-arm leaderboard). Which arms should we "
        "drop or tune, and what specific parameter would you change?\"*"
    )
    L.append(
        "2. *\"Look at section 4 (funnel). What does the dominant bucket "
        "tell us about the bottleneck in the pipeline?\"*"
    )
    L.append(
        "3. *\"Look at the trade traces in section 5. For the worst losers, "
        "was the bandit's choice defensible given the context? Was the sizer "
        "too aggressive?\"*"
    )
    L.append(
        "4. *\"Section 6 lists symbols we missed. Which bucket dominates, and "
        "what change would help us catch them next time?\"*"
    )
    L.append("")
    L.append(
        "_Companion CSV with one row per trade is in the same directory — "
        "drop both files into a single Claude conversation for joint analysis._"
    )

    return "\n".join(L)


# ---------------------------------------------------------------------------
# Entry point — orchestrates I/O + writes the two output files
# ---------------------------------------------------------------------------

def _resolve_symbol(skel: SessionSkeleton, instrument_id: uuid.UUID) -> str:
    """Map an instrument_id back to its symbol via the skeleton's universe."""
    for u in skel.universe:
        if u.instrument_id == instrument_id:
            return u.symbol
    return f"<unknown {str(instrument_id)[:8]}>"


async def main_async(args: argparse.Namespace) -> int:
    runs = await list_runs(args.portfolio_id, limit=200)
    runs_in_range = [
        r for r in runs
        if args.start_date <= r.backtest_date <= args.end_date
    ]
    if not runs_in_range:
        print(
            f"No backtest_runs found for portfolio {args.portfolio_id} in "
            f"{args.start_date}..{args.end_date}.",
            file=sys.stderr,
        )
        return 1

    # 1. Load skeletons (universe + per-tick summary + trades)
    skeletons: list[SessionSkeleton] = []
    for r in runs_in_range:
        skel = await load_session_skeleton(r.run_id)
        if skel is not None:
            skeletons.append(skel)
    if not skeletons:
        print("All skeletons failed to load.", file=sys.stderr)
        return 1
    skeletons.sort(key=lambda s: s.metadata.backtest_date)

    # 2. Build the (skel, trade, symbol) triples — needed for arm stats,
    #    CSV, and trace rendering.
    trades_with_meta: list[tuple[SessionSkeleton, TradeRecord, str]] = []
    for skel in skeletons:
        for t in skel.trades:
            sym = _resolve_symbol(skel, t.underlying_id)
            trades_with_meta.append((skel, t, sym))
    all_trades = [t for _, t, _ in trades_with_meta]

    # 3. Per-arm leaderboard + funnel aggregates (pure)
    arm_stats = _compute_arm_stats(all_trades)
    funnel_counts = _aggregate_funnel_buckets(skeletons)

    # 4. Pick + render trace samples (top winners + worst losers)
    picks = _pick_trace_samples(
        all_trades, k_winners=args.top_winners, k_losers=args.top_losers,
    )
    trace_renderings: list[str] = []
    for trade in picks:
        # Find which skeleton this trade lives in + its symbol
        owning = next((s for s, t, _ in trades_with_meta if t.trade_id == trade.trade_id), None)
        symbol = next((sym for s, t, sym in trades_with_meta if t.trade_id == trade.trade_id), "")
        if owning is None:
            continue
        focused = await load_tick_state(owning.metadata.run_id, trade.entry_at, symbol=symbol)
        full = await load_tick_state(owning.metadata.run_id, trade.entry_at, symbol=None)
        if focused is None or full is None:
            continue
        trace_renderings.append(
            _render_trade_trace(
                skel=owning, trade=trade, focused=focused, full=full, symbol=symbol,
            )
        )

    # 5. Missed top-gainers from the LATEST day in range (one day's worth
    #    is plenty for the LLM; the funnel-summary already covers cross-range).
    missed_funnels: list[FunnelRow] = []
    if skeletons:
        latest = skeletons[-1]
        async with session_scope() as session:
            gainers = await _load_top_gainers(
                session, latest.metadata.backtest_date, top_n=args.top_missed,
            )
            sig_logs = await _load_signal_logs_by_symbol(session, latest.metadata.run_id)
            trades_by_uid = await _load_trades_by_underlying(session, latest.metadata.run_id)
        universe_syms = {u.symbol for u in latest.universe}
        for g in gainers:
            missed_funnels.append(
                _classify(
                    gainer=g,
                    universe_symbols=universe_syms,
                    signal_logs=sig_logs.get(g.symbol, []),
                    trades=trades_by_uid.get(g.instrument_id, []),
                )
            )

    # 6. Build outputs
    md = _build_markdown(
        portfolio_id=args.portfolio_id,
        start=args.start_date,
        end=args.end_date,
        skeletons=skeletons,
        all_trades_with_meta=trades_with_meta,
        arm_stats=arm_stats,
        funnel_counts=funnel_counts,
        trace_renderings=trace_renderings,
        missed_funnels=missed_funnels,
    )
    csv_text = _trades_csv_text(trades_with_meta)

    out_md = args.out or Path(
        f"reports/analyst_pack_{args.start_date}_{args.end_date}.md"
    )
    out_csv = args.csv_out or Path(
        f"reports/trades_{args.start_date}_{args.end_date}.csv"
    )
    out_md = Path(out_md)
    out_csv = Path(out_csv)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(md, encoding="utf-8")
    out_csv.write_text(csv_text, encoding="utf-8")

    print(f"Markdown: {out_md}  ({len(md):,} bytes)")
    print(f"CSV:      {out_csv}  ({len(csv_text):,} bytes)")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _parse_uuid(s: str) -> uuid.UUID:
    try:
        return uuid.UUID(s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Not a valid UUID: {s!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="analyst_pack",
        description="Single-markdown summary of a backtest range, plus a "
                    "trades CSV. Designed for LLM ingestion (Claude Desktop).",
    )
    p.add_argument("--start-date", type=_parse_date, required=True)
    p.add_argument("--end-date", type=_parse_date, required=True)
    p.add_argument("--portfolio-id", type=_parse_uuid, required=True)
    p.add_argument("--out", type=Path, default=None,
                   help="Output markdown path (default: reports/analyst_pack_<dates>.md)")
    p.add_argument("--csv-out", type=Path, default=None,
                   help="Output CSV path (default: reports/trades_<dates>.csv)")
    p.add_argument("--top-winners", type=int, default=3,
                   help="How many top-PnL trades to render with full traces (default 3)")
    p.add_argument("--top-losers", type=int, default=3,
                   help="How many worst-PnL trades to render with full traces (default 3)")
    p.add_argument("--top-missed", type=int, default=10,
                   help="How many missed top-gainers to surface from the latest day (default 10)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.end_date < args.start_date:
        print("--end-date must be on or after --start-date", file=sys.stderr)
        return 2
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
