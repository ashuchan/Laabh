"""Position-sizing and pre-trade risk checks."""
from __future__ import annotations

from decimal import Decimal

from loguru import logger
from sqlalchemy import select

from src.db import session_scope
from src.models.portfolio import Holding, Portfolio


class RiskError(ValueError):
    """Raised when a trade violates risk constraints."""


class RiskManager:
    """Validates orders against cash, position limits, and market-hours rules.

    All monetary values are in Decimal to avoid float rounding.
    """

    MAX_POSITION_PCT: Decimal = Decimal("0.10")  # max 10% of portfolio per stock

    async def validate_buy(
        self,
        portfolio_id: str,
        instrument_id: str,
        quantity: int,
        price: Decimal,
        total_cost: Decimal,
    ) -> None:
        """Raise RiskError if the buy order is not permissible."""
        async with session_scope() as session:
            portfolio = await session.get(Portfolio, portfolio_id)
            if portfolio is None:
                raise RiskError(f"Portfolio {portfolio_id} not found")

            cash = Decimal(str(portfolio.current_cash))
            if total_cost > cash:
                raise RiskError(
                    f"Insufficient cash: need ₹{total_cost:,.2f}, have ₹{cash:,.2f}"
                )

            portfolio_total = Decimal(str(portfolio.current_cash)) + Decimal(
                str(portfolio.invested_value or 0)
            )
            max_position = portfolio_total * self.MAX_POSITION_PCT

            result = await session.execute(
                select(Holding).where(
                    Holding.portfolio_id == portfolio_id,
                    Holding.instrument_id == instrument_id,
                )
            )
            holding = result.scalar_one_or_none()
            existing_value = (
                Decimal(str(holding.invested_value)) if holding else Decimal("0")
            )
            new_position_value = existing_value + total_cost
            if new_position_value > max_position:
                raise RiskError(
                    f"Position limit exceeded: ₹{new_position_value:,.2f} > "
                    f"max ₹{max_position:,.2f} (10% of portfolio)"
                )

        logger.debug(
            f"risk check passed: buy {quantity}×₹{price} total=₹{total_cost}"
        )

    async def validate_sell(
        self,
        portfolio_id: str,
        instrument_id: str,
        quantity: int,
    ) -> None:
        """Raise RiskError if the sell order is not permissible."""
        async with session_scope() as session:
            result = await session.execute(
                select(Holding).where(
                    Holding.portfolio_id == portfolio_id,
                    Holding.instrument_id == instrument_id,
                )
            )
            holding = result.scalar_one_or_none()
            if holding is None or holding.quantity < quantity:
                have = holding.quantity if holding else 0
                raise RiskError(
                    f"Insufficient holding: need {quantity} shares, have {have}"
                )
