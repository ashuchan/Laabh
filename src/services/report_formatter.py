"""Telegram-ready formatter for the combined daily P&L snapshot.

One source of truth (``pnl_aggregator.daily_pnl_snapshot``) feeds the same
formatted block into:
  * 15:40 IST F&O EOD message (replaces the old phase-counts-only digest)
  * 18:30 IST daily report
  * Morning + EOD square-off Telegram digests (via composition)

Output is plain-text with a single triple-backtick block for the holdings
table so columns align in monospaced Telegram. Sent with
``parse_mode='Markdown'`` (legacy) — MarkdownV2 escaping is not used here
because the body is fully constructed by us (no LLM-emitted free-form text)
and the legacy parser is more forgiving.
"""
from __future__ import annotations

from src.services.pnl_aggregator import DailyPnLSnapshot, StrategyBucketPnL

_BUCKET_LABELS = {
    "equity": "Equity LLM",
    "fno_directional": "FNO Directional",
    "fno_spread": "FNO Spreads",
    "fno_volatility": "FNO Volatility",
}
_BUCKET_ORDER = ("equity", "fno_directional", "fno_spread", "fno_volatility")


def _sign(v: float) -> str:
    return "+" if v >= 0 else ""


def _emoji_for_pnl(v: float, *, has_activity: bool) -> str:
    if not has_activity:
        return "⚪"
    if v > 0:
        return "🟢"
    if v < 0:
        return "🔴"
    return "🟡"


def format_combined_eod_report(snap: DailyPnLSnapshot, *, title: str) -> str:
    """Render the unified Telegram message for an EOD-style firing.

    Layout (top to bottom):

    1. Headline: net day P&L in ₹ and %
    2. Realized vs unrealized split
    3. Per-bucket table: budget cap • deployed • day P&L • emoji status
    4. Closed trades today (winners + losers, capped at 12 lines)
    5. F&O pipeline stats (Phase 1/2/3 counts) for context
    6. Capital position: pool size, deployed, cash remaining

    The triple-backtick code block holds the per-bucket table so columns
    align. Senders should use ``parse_mode='Markdown'`` (legacy).
    """
    lines: list[str] = [f"📊 *{title}*"]

    pnl = snap.day_pnl_total
    pnl_pct = snap.day_pnl_pct
    pct_str = f" ({_sign(pnl_pct)}{pnl_pct:.2f}%)" if pnl_pct is not None else ""
    pnl_icon = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "🟡")
    lines.append(
        f"{pnl_icon} *Day P&L: {_sign(pnl)}₹{pnl:,.0f}{pct_str}*"
    )
    lines.append(
        f"  Realized:    {_sign(snap.realized_pnl_total)}₹{snap.realized_pnl_total:,.0f}"
    )
    lines.append(
        f"  Unrealized:  {_sign(snap.unrealized_pnl_total)}₹{snap.unrealized_pnl_total:,.0f}"
    )
    lines.append("")

    # Per-bucket table (monospaced)
    name_w, cap_w, dep_w, pnl_w, fills_w = 18, 9, 9, 11, 7
    head = (
        "STRATEGY".ljust(name_w) + " " +
        "CAP".rjust(cap_w) + " " +
        "DEP".rjust(dep_w) + " " +
        "DAY P&L".rjust(pnl_w) + " " +
        "FILLS".rjust(fills_w)
    )
    sep = "-" * len(head)
    table_rows = [head, sep]
    for key in _BUCKET_ORDER:
        b: StrategyBucketPnL = snap.buckets[key]
        has_activity = b.fills_count > 0 or b.closes_count > 0 or b.open_count > 0
        emoji = _emoji_for_pnl(b.day_pnl, has_activity=has_activity)
        name = f"{emoji} {_BUCKET_LABELS[key]}"
        pnl_str = f"{_sign(b.day_pnl)}{b.day_pnl:,.0f}"
        table_rows.append(
            name.ljust(name_w) + " " +
            f"{b.rupee_cap:,.0f}".rjust(cap_w) + " " +
            f"{b.deployed:,.0f}".rjust(dep_w) + " " +
            pnl_str.rjust(pnl_w) + " " +
            f"{b.fills_count}".rjust(fills_w)
        )
    lines.append("```")
    lines.extend(table_rows)
    lines.append("```")

    # Closed trades today
    if snap.closed_trades:
        winners = [t for t in snap.closed_trades if t.pnl > 0]
        losers = [t for t in snap.closed_trades if t.pnl < 0]
        winners.sort(key=lambda t: t.pnl, reverse=True)
        losers.sort(key=lambda t: t.pnl)
        lines.append(f"*Closed today ({len(snap.closed_trades)}):*")
        for t in winners[:6]:
            lines.append(
                f"  ✅ {t.reason} {t.label}: +₹{t.pnl:,.0f}"
            )
        for t in losers[:6]:
            lines.append(
                f"  🛑 {t.reason} {t.label}: ₹{t.pnl:,.0f}"
            )
        spillover = max(0, len(snap.closed_trades) - 12)
        if spillover:
            lines.append(f"  …and {spillover} more")
        lines.append("")

    # FNO pipeline context
    if snap.fno_phase1_passed or snap.fno_phase2_passed or snap.fno_phase3_proceed:
        lines.append(
            f"*F&O pipeline:* P1 {snap.fno_phase1_passed}, "
            f"P2 {snap.fno_phase2_passed}, P3 PROCEED {snap.fno_phase3_proceed}"
        )

    # Capital position
    pool = snap.plan.total_budget
    deployed_total = sum(b.deployed for b in snap.buckets.values())
    deployed_pct = (deployed_total / pool * 100.0) if pool > 0 else 0.0
    lines.append(
        f"*Capital pool:* ₹{pool:,.0f}  •  "
        f"Deployed today: ₹{deployed_total:,.0f} ({deployed_pct:.0f}%)  •  "
        f"Cash: ₹{snap.cash_remaining:,.0f}"
    )
    if snap.plan.source == "default":
        lines.append("_Allocation: defaults (LLM allocator did not run)._")
    elif snap.plan.source == "llm":
        lines.append(f"_Allocation: LLM @ {snap.plan.decided_at:%H:%M UTC}._")

    return "\n".join(lines)


def format_compact_pnl_block(snap: DailyPnLSnapshot) -> str:
    """One-paragraph variant for splicing into per-cron digests.

    Used by the morning + EOD square-off summaries so they share the same
    headline numbers as the EOD report. Three lines: headline P&L, per-
    bucket day P&L, and capital position.
    """
    pnl = snap.day_pnl_total
    pct = snap.day_pnl_pct
    pct_str = f" ({_sign(pct)}{pct:.2f}%)" if pct is not None else ""
    parts: list[str] = [
        f"Day P&L: {_sign(pnl)}₹{pnl:,.0f}{pct_str} "
        f"(realized {_sign(snap.realized_pnl_total)}₹{snap.realized_pnl_total:,.0f}, "
        f"open {_sign(snap.unrealized_pnl_total)}₹{snap.unrealized_pnl_total:,.0f})"
    ]
    bucket_bits = []
    for key in _BUCKET_ORDER:
        b = snap.buckets[key]
        if b.fills_count == 0 and b.closes_count == 0 and b.open_count == 0:
            continue
        bucket_bits.append(
            f"{_BUCKET_LABELS[key]} {_sign(b.day_pnl)}₹{b.day_pnl:,.0f}"
        )
    if bucket_bits:
        parts.append("By strategy: " + " · ".join(bucket_bits))
    deployed = sum(b.deployed for b in snap.buckets.values())
    parts.append(
        f"Pool ₹{snap.plan.total_budget:,.0f} • "
        f"deployed ₹{deployed:,.0f} • cash ₹{snap.cash_remaining:,.0f}"
    )
    return "\n".join(parts)


__all__ = ["format_combined_eod_report", "format_compact_pnl_block"]
