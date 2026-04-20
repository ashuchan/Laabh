"""Daily and weekly performance reports — formatted for Telegram delivery."""
from __future__ import annotations

from datetime import date, datetime, timezone

from loguru import logger
from sqlalchemy import select, text

from src.db import session_scope
from src.models.analyst import Analyst
from src.models.portfolio import Holding, Portfolio
from src.models.signal import Signal
from src.services.notification_service import NotificationService


class ReportGenerator:
    """Generates daily Telegram-formatted performance reports."""

    def __init__(self) -> None:
        self._notifier = NotificationService()

    async def send_daily_report(self) -> None:
        """Build and send today's portfolio + signal summary via Telegram."""
        report = await self._build_report()
        await self._notifier._send_telegram(report)
        logger.info("daily report sent")

    async def _build_report(self) -> str:
        today = date.today()
        lines: list[str] = [f"📊 Laabh Daily Report — {today.strftime('%d %b %Y')}", ""]

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

        return "\n".join(lines)
