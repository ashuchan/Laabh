"""Daily and weekly performance reports — formatted for Telegram delivery."""
from __future__ import annotations

from datetime import date, datetime, time, timezone

from loguru import logger
from sqlalchemy import desc, select, text

from src.db import session_scope
from src.models.analyst import Analyst
from src.models.instrument import Instrument
from src.models.portfolio import Holding, Portfolio
from src.models.signal import Signal
from src.models.strategy_decision import StrategyDecision
from src.models.trade import Trade
from src.services.notification_service import NotificationService


class ReportGenerator:
    """Generates daily Telegram-formatted performance reports."""

    def __init__(self) -> None:
        self._notifier = NotificationService()

    async def send_daily_report(self) -> None:
        """Build and send today's portfolio + signal summary via Telegram.

        The report opens with the unified P&L block (same shape as the 15:40
        IST F&O EOD message) so both messages reconcile to the same numbers.
        Sent with legacy Markdown so the monospaced bucket table renders.
        """
        report = await self._build_report()
        await self._notifier._send_telegram(report, parse_mode="Markdown")
        logger.info("daily report sent")

    async def _build_report(self) -> str:
        from src.services.pnl_aggregator import daily_pnl_snapshot
        from src.services.report_formatter import format_combined_eod_report

        today = date.today()

        # Lead with the unified equity + F&O P&L block. This is the single
        # source of truth shared with the 15:40 F&O EOD message; both
        # numbers will line up because they read from the same aggregator.
        snap = await daily_pnl_snapshot(today=today)
        lines: list[str] = [
            format_combined_eod_report(
                snap, title=f"Laabh Daily Report — {today:%d %b %Y}"
            ),
            "",
        ]

        # Portfolio summary
        async with session_scope() as session:
            result = await session.execute(
                select(Portfolio).where(Portfolio.is_active == True).limit(1)
            )
            portfolio = result.scalar_one_or_none()

        if portfolio:
            total = portfolio.current_value or 0
            cash = portfolio.current_cash or 0
            pnl = portfolio.total_pnl or 0
            pnl_pct = portfolio.total_pnl_pct or 0
            day_pnl = portfolio.day_pnl or 0
            sign = "+" if pnl >= 0 else ""
            lines.append(
                f"Portfolio: ₹{total + cash:,.0f} ({sign}₹{pnl:,.0f} / {sign}{pnl_pct:.1f}%)"
            )

            # Holdings
            async with session_scope() as session:
                result = await session.execute(
                    select(Holding).where(Holding.portfolio_id == portfolio.id)
                )
                holdings = result.scalars().all()

            if holdings:
                lines.append(f"\nHoldings ({len(holdings)}):")
                for h in sorted(holdings, key=lambda x: (x.pnl or 0), reverse=True)[:5]:
                    p = h.pnl or 0
                    pp = h.pnl_pct or 0
                    s = "+" if p >= 0 else ""
                    lines.append(
                        f"  {h.instrument_id}  {h.quantity} qty  {s}₹{p:,.0f} ({s}{pp:.1f}%)"
                    )

        # Signals today
        async with session_scope() as session:
            result = await session.execute(
                select(Signal).where(
                    Signal.signal_date >= datetime.now(timezone.utc).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                )
            )
            todays_signals = result.scalars().all()

        buys = sum(1 for s in todays_signals if s.action == "BUY")
        sells = sum(1 for s in todays_signals if s.action == "SELL")
        holds = sum(1 for s in todays_signals if s.action == "HOLD")
        lines.append(f"\nSignals Today: {len(todays_signals)} new ({buys} BUY, {sells} SELL, {holds} HOLD)")

        # Top signal by confidence
        top = max(todays_signals, key=lambda s: (s.convergence_score or 0, s.confidence or 0), default=None)
        if top:
            lines.append(
                f"Top Signal: {top.action} instrument @ entry={top.entry_price} → target={top.target_price}"
            )

        # Analyst of the day
        async with session_scope() as session:
            result = await session.execute(
                select(Analyst)
                .where(Analyst.credibility_score > 0)
                .order_by(Analyst.credibility_score.desc())
                .limit(1)
            )
            top_analyst = result.scalar_one_or_none()

        if top_analyst:
            hit_rate_pct = int((top_analyst.hit_rate or 0) * 100) if hasattr(top_analyst, "hit_rate") else "?"
            lines.append(
                f"\nAnalyst of the Day: {top_analyst.name} "
                f"(credibility: {(top_analyst.credibility_score or 0):.2f}, "
                f"hit rate: {hit_rate_pct}%, "
                f"{top_analyst.total_signals or 0} signals)"
            )

        # Equity strategy section
        strategy_lines = await self._build_equity_strategy_section(today)
        if strategy_lines:
            lines.append("")
            lines.extend(strategy_lines)

        return "\n".join(lines)

    async def _build_equity_strategy_section(self, today: date) -> list[str]:
        """Summarise the LLM-driven equity strategy for today.

        Reports morning allocation rationale, every fill, intraday turnover,
        EOD square-off rationale, and the strategy portfolio's day P&L.
        Returns an empty list when the strategy hasn't run today (so the
        section disappears for users who haven't enabled it).
        """
        from src.trading.strategy_runner import STRATEGY_PORTFOLIO_NAME

        async with session_scope() as session:
            result = await session.execute(
                select(Portfolio).where(Portfolio.name == STRATEGY_PORTFOLIO_NAME)
            )
            portfolio = result.scalar_one_or_none()
        if portfolio is None:
            return []

        day_start = datetime.combine(today, time.min, tzinfo=timezone.utc)
        day_end = datetime.combine(today, time.max, tzinfo=timezone.utc)

        async with session_scope() as session:
            decisions = list(
                (await session.execute(
                    select(StrategyDecision)
                    .where(
                        StrategyDecision.portfolio_id == portfolio.id,
                        StrategyDecision.as_of >= day_start,
                        StrategyDecision.as_of <= day_end,
                    )
                    .order_by(StrategyDecision.as_of.asc())
                )).scalars()
            )
            trade_rows = list(
                (await session.execute(
                    select(Trade, Instrument)
                    .join(Instrument, Instrument.id == Trade.instrument_id)
                    .where(
                        Trade.portfolio_id == portfolio.id,
                        Trade.executed_at >= day_start,
                        Trade.executed_at <= day_end,
                    )
                    .order_by(Trade.executed_at.asc())
                )).all()
            )

        if not decisions and not trade_rows:
            return []

        out: list[str] = ["📈 Equity Strategy"]

        morning = next((d for d in decisions if d.decision_type == "morning_allocation"), None)
        intraday = [d for d in decisions if d.decision_type == "intraday_action"]
        eod = next((d for d in decisions if d.decision_type == "eod_squareoff"), None)

        if morning:
            executed = morning.actions_executed or 0
            skipped = morning.actions_skipped or 0
            out.append(
                f"  Morning: {executed} fills, {skipped} skipped "
                f"(budget ₹{(morning.budget_available or 0):,.0f})"
            )
            if morning.llm_reasoning:
                out.append(f"    _{morning.llm_reasoning[:240]}_")

        if intraday:
            total_exec = sum((d.actions_executed or 0) for d in intraday)
            total_skip = sum((d.actions_skipped or 0) for d in intraday)
            out.append(
                f"  Intraday: {len(intraday)} re-evals, {total_exec} fills, {total_skip} skipped"
            )

        if eod:
            executed = eod.actions_executed or 0
            out.append(f"  Square-off: {executed} positions closed")
            if eod.llm_reasoning:
                out.append(f"    _{eod.llm_reasoning[:240]}_")

        if trade_rows:
            out.append("  Fills:")
            for t, inst in trade_rows[:15]:
                tag = "🟢" if t.trade_type == "BUY" else "🔴"
                out.append(
                    f"    {tag} {t.trade_type} {t.quantity} {inst.symbol} "
                    f"@ ₹{float(t.price):,.2f}"
                )
            if len(trade_rows) > 15:
                out.append(f"    … {len(trade_rows) - 15} more")

        cash = float(portfolio.current_cash or 0)
        invested = float(portfolio.invested_value or 0)
        day_pnl = float(portfolio.day_pnl or 0)
        sign = "+" if day_pnl >= 0 else ""
        out.append(
            f"  Portfolio: cash ₹{cash:,.0f} • invested ₹{invested:,.0f} • "
            f"day {sign}₹{day_pnl:,.0f}"
        )
        return out
