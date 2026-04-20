"""Portfolio valuation — P&L recalculation and daily snapshots."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from loguru import logger
from sqlalchemy import select, text

from src.db import session_scope
from src.models.portfolio import Holding, Portfolio, PortfolioSnapshot
from src.models.price import PriceTick, PriceDaily


class PortfolioManager:
    """Recalculates portfolio P&L and stores EOD snapshots."""

    async def update_all_portfolios(self) -> None:
        """Recalculate current values for every active portfolio."""
        async with session_scope() as session:
            result = await session.execute(
                select(Portfolio).where(Portfolio.is_active == True)
            )
            portfolios = result.scalars().all()

        for portfolio in portfolios:
            try:
                await self._update_portfolio(portfolio.id)
            except Exception as exc:
                logger.error(f"portfolio update failed for {portfolio.id}: {exc}")

    async def _update_portfolio(self, portfolio_id: uuid.UUID) -> None:
        async with session_scope() as session:
            portfolio = await session.get(Portfolio, portfolio_id)
            if not portfolio:
                return

            result = await session.execute(
                select(Holding).where(Holding.portfolio_id == portfolio_id)
            )
            holdings = result.scalars().all()

            total_current = Decimal("0")
            total_invested = Decimal("0")
            total_day_pnl = Decimal("0")

            for holding in holdings:
                ltp = await self._get_ltp(holding.instrument_id)
                prev_close = await self._get_prev_close(holding.instrument_id)

                if ltp is not None:
                    qty = holding.quantity
                    current_val = ltp * qty
                    inv_val = Decimal(str(holding.invested_value))
                    pnl = current_val - inv_val
                    pnl_pct = (pnl / inv_val * 100) if inv_val else Decimal("0")

                    holding.current_price = float(ltp)
                    holding.current_value = float(current_val)
                    holding.pnl = float(pnl)
                    holding.pnl_pct = float(pnl_pct)

                    if prev_close:
                        day_change = (ltp - prev_close) / prev_close * 100
                        holding.day_change_pct = float(day_change)
                        total_day_pnl += (ltp - prev_close) * qty

                    total_current += current_val
                    total_invested += Decimal(str(holding.invested_value))

            cash = Decimal(str(portfolio.current_cash))
            portfolio_total = total_current + cash
            total_pnl = total_current - total_invested

            initial = Decimal(str(portfolio.initial_capital))
            portfolio.invested_value = float(total_invested)
            portfolio.current_value = float(total_current)
            portfolio.total_pnl = float(total_pnl)
            portfolio.total_pnl_pct = float(
                (total_pnl / initial * 100) if initial else Decimal("0")
            )
            portfolio.day_pnl = float(total_day_pnl)

            # Update weight_pct for each holding
            for holding in holdings:
                if holding.current_value and portfolio_total > 0:
                    holding.weight_pct = float(
                        Decimal(str(holding.current_value)) / portfolio_total * 100
                    )

    async def take_snapshot(self, portfolio_id: uuid.UUID) -> PortfolioSnapshot:
        """Persist an EOD portfolio snapshot."""
        today = date.today()
        async with session_scope() as session:
            portfolio = await session.get(Portfolio, portfolio_id)
            if not portfolio:
                raise ValueError(f"Portfolio {portfolio_id} not found")

            result = await session.execute(
                select(Holding).where(Holding.portfolio_id == portfolio_id)
            )
            holdings = result.scalars().all()
            num_holdings = len(holdings)

            snap = PortfolioSnapshot(
                portfolio_id=portfolio_id,
                date=today,
                total_value=portfolio.current_value,
                cash=portfolio.current_cash,
                invested_value=portfolio.invested_value,
                day_pnl=portfolio.day_pnl,
                day_pnl_pct=(
                    portfolio.day_pnl / portfolio.current_value * 100
                    if portfolio.current_value
                    else 0
                ),
                cumulative_pnl_pct=portfolio.total_pnl_pct,
                num_holdings=num_holdings,
            )
            session.add(snap)
        logger.info(f"portfolio snapshot taken for {portfolio_id} on {today}")
        return snap

    async def _get_ltp(self, instrument_id: uuid.UUID) -> Decimal | None:
        async with session_scope() as session:
            result = await session.execute(
                select(PriceTick.ltp)
                .where(PriceTick.instrument_id == instrument_id)
                .order_by(PriceTick.timestamp.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
        if row is not None:
            return Decimal(str(row))
        # Fallback to daily close
        async with session_scope() as session:
            result = await session.execute(
                select(PriceDaily.close)
                .where(PriceDaily.instrument_id == instrument_id)
                .order_by(PriceDaily.date.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
        return Decimal(str(row)) if row is not None else None

    async def _get_prev_close(self, instrument_id: uuid.UUID) -> Decimal | None:
        async with session_scope() as session:
            result = await session.execute(
                select(PriceDaily.close)
                .where(PriceDaily.instrument_id == instrument_id)
                .order_by(PriceDaily.date.desc())
                .offset(1)
                .limit(1)
            )
            row = result.scalar_one_or_none()
        return Decimal(str(row)) if row is not None else None
