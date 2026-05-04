"""F&O notification formatter — renders trade alerts for Telegram.

Builds human-readable Telegram messages for F&O events:
  - New signal (PROCEED decision from Phase 3)
  - Trade entry (simulated fill confirmed)
  - Stop-loss hit
  - Target hit
  - Hard exit (end-of-day force close)
  - Phase 1/2 summary (daily digest)

All messages use Telegram MarkdownV2 formatting (escaped as needed).
The actual send is delegated to `src.services.notification_service`.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from src.fno.intraday_manager import OpenPosition
from src.fno.strategies.base import StrategyRecommendation

# Telegram MarkdownV2 special chars that must be escaped
_MD_SPECIAL = r"\_*[]()~`>#+-=|{}.!"


def _escape(text: str) -> str:
    """Escape MarkdownV2 special characters."""
    for ch in _MD_SPECIAL:
        text = text.replace(ch, f"\\{ch}")
    return text


def _direction_emoji(direction: str) -> str:
    return {"bullish": "🟢", "bearish": "🔴", "neutral": "🟡"}.get(direction, "⚪")


def format_signal_alert(
    symbol: str,
    direction: str,
    thesis: str,
    confidence: float,
    composite_score: float,
    strategy_name: str,
    iv_regime: str,
    iv_rank: float | None,
) -> str:
    """Format a new F&O signal alert."""
    emoji = _direction_emoji(direction)
    conf_pct = f"{confidence * 100:.0f}%"
    iv_str = f"{iv_rank:.0f}%" if iv_rank is not None else "N/A"
    return (
        f"{emoji} *F&O Signal: {_escape(symbol)}*\n"
        f"Direction: {_escape(direction.upper())}\n"
        f"Strategy: {_escape(strategy_name.replace('_', ' ').title())}\n"
        f"IV Regime: {_escape(iv_regime)} \\(Rank: {_escape(iv_str)}\\)\n"
        f"Composite Score: {_escape(str(round(composite_score, 1)))}/10\n"
        f"Confidence: {_escape(conf_pct)}\n"
        f"Thesis: _{_escape(thesis[:200])}_"
    )


def format_entry_alert(
    symbol: str,
    strategy_name: str,
    fill_price: Decimal,
    strike: Decimal,
    option_type: str,
    lots: int,
    stop_price: Decimal,
    target_price: Decimal,
) -> str:
    """Format a trade entry confirmation."""
    return (
        f"✅ *Entry: {_escape(symbol)}*\n"
        f"Strategy: {_escape(strategy_name.replace('_', ' ').title())}\n"
        f"{_escape(option_type)} Strike: ₹{_escape(str(strike))}\n"
        f"Fill Price: ₹{_escape(str(fill_price))}\n"
        f"Lots: {_escape(str(lots))}\n"
        f"Stop: ₹{_escape(str(stop_price))} \\| Target: ₹{_escape(str(target_price))}"
    )


def format_stop_alert(
    symbol: str,
    exit_price: Decimal,
    entry_price: Decimal,
    pnl: Decimal,
) -> str:
    """Format a stop-loss exit alert."""
    pnl_sign = "\\+" if pnl >= 0 else ""
    return (
        f"🛑 *Stop Hit: {_escape(symbol)}*\n"
        f"Exit: ₹{_escape(str(exit_price))} \\| Entry was: ₹{_escape(str(entry_price))}\n"
        f"P&L: {pnl_sign}{_escape(str(pnl))}"
    )


def format_target_alert(
    symbol: str,
    exit_price: Decimal,
    entry_price: Decimal,
    pnl: Decimal,
) -> str:
    """Format a profit-target exit alert."""
    return (
        f"🎯 *Target Hit: {_escape(symbol)}*\n"
        f"Exit: ₹{_escape(str(exit_price))} \\| Entry was: ₹{_escape(str(entry_price))}\n"
        f"P&L: \\+{_escape(str(pnl))}"
    )


def format_hard_exit_alert(
    symbol: str,
    exit_price: Decimal,
    entry_price: Decimal,
    pnl: Decimal,
) -> str:
    """Format a hard intraday exit at 14:30."""
    pnl_sign = "\\+" if pnl >= 0 else ""
    return (
        f"⏰ *Hard Exit: {_escape(symbol)}*\n"
        f"Closed at 14:30 IST \\| Exit: ₹{_escape(str(exit_price))}\n"
        f"P&L: {pnl_sign}{_escape(str(pnl))}"
    )


def format_daily_summary(
    run_date: str,
    phase1_passed: int,
    phase2_passed: int,
    phase3_proceed: int,
    trades_entered: int,
    net_pnl: Decimal,
) -> str:
    """Format end-of-day F&O pipeline summary."""
    pnl_sign = "\\+" if net_pnl >= 0 else ""
    return (
        f"📊 *F&O Daily Summary \\({_escape(run_date)}\\)*\n"
        f"Phase 1 \\(liquidity\\): {_escape(str(phase1_passed))} passed\n"
        f"Phase 2 \\(catalyst\\): {_escape(str(phase2_passed))} passed\n"
        f"Phase 3 \\(PROCEED\\): {_escape(str(phase3_proceed))}\n"
        f"Trades entered: {_escape(str(trades_entered))}\n"
        f"Net P&L: {pnl_sign}{_escape(str(net_pnl))}"
    )


def format_morning_brief(
    run_date: str,
    candidates: list[dict],
) -> str:
    """Format the pre-open morning brief listing PROCEED candidates.

    Each candidate dict needs: symbol, direction, strategy, composite_score,
    iv_regime, thesis. Optionally:
      - contract: pre-formatted leg label (e.g. "BUY CE 1700 @ ₹42.50 (exp 26-May)")
      - underlying_ltp, target_premium, stop_premium for richer rendering.
    List may be empty (no Phase 3 PROCEEDs that day).
    """
    header = f"🌅 *F&O Morning Brief \\({_escape(run_date)}\\)*\n"
    if not candidates:
        return header + "_No Phase 3 PROCEED candidates for today\\._"

    lines = [header, f"{_escape(str(len(candidates)))} candidate\\(s\\) ready:"]
    for i, c in enumerate(candidates, 1):
        emoji = _direction_emoji(c.get("direction", "neutral"))
        sym = _escape(str(c.get("symbol", "?")))
        strat = _escape(str(c.get("strategy", "")).replace("_", " ").title())
        score = _escape(f"{float(c.get('composite_score') or 0):.1f}")
        iv = _escape(str(c.get("iv_regime", "n/a")))
        thesis = _escape(str(c.get("thesis", ""))[:140])

        block = (
            f"\n*{_escape(str(i))}\\. {emoji} {sym}*  "
            f"\\(score: {score}, IV: {iv}\\)\n"
            f"  Strategy: {strat}\n"
        )
        if c.get("contract"):
            block += f"  Contract: `{_escape(str(c['contract']))}`\n"
            tgt = c.get("target_premium")
            stop = c.get("stop_premium")
            if tgt and stop:
                block += (
                    f"  Target: ₹{_escape(str(tgt))} \\| "
                    f"Stop: ₹{_escape(str(stop))}\n"
                )
        if c.get("underlying_ltp"):
            block += f"  Underlying: ₹{_escape(str(c['underlying_ltp']))}\n"
        block += f"  _{thesis}_"
        lines.append(block)
    return "".join(lines)
