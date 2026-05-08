"""Core paper-trade execution logic — market orders only (limit/SL via order_book)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from loguru import logger
from sqlalchemy import select, text

from src.config import get_settings
from src.db import session_scope
from src.models.portfolio import Holding, Portfolio
from src.models.trade import Trade
from src.models.instrument import Instrument
from src.services.notification_service import NotificationService
from src.trading.risk_manager import RiskManager, RiskError


class EquityTradingDisabled(Exception):
    """Raised when an equity (non-F&O) trade is attempted while
    ``EQUITY_TRADING_ENABLED=false``.

    Distinct from ``RiskError`` so callers can map it to a 403 instead of a
    422 — this is a policy refusal, not a risk-rule violation.
    """


_TWO = Decimal("0.01")


async def _refuse_if_equity_disabled(
    instrument_id: uuid.UUID,
    *,
    trade_type: str,
    quantity: int,
) -> None:
    """Raise ``EquityTradingDisabled`` if the master flag is off and the
    instrument is an equity (``is_fno=False``).

    Invoked at every trade-execution chokepoint (engine market order,
    order-book limit/SL placement). When the flag is True (default) this
    is a no-op — no DB roundtrip.
    """
    settings = get_settings()
    if settings.equity_trading_enabled:
        return
    # Read the two attributes we need *inside* the session scope. After the
    # ``async with`` exits, ``session.commit()`` would expire attributes on
    # the default SQLAlchemy config, and a later ``instr.is_fno`` access
    # would attempt a lazy refresh against a closed session. Today this
    # works only because ``src/db.py`` sets ``expire_on_commit=False`` —
    # don't depend on that here.
    async with session_scope() as session:
        instr = await session.get(Instrument, instrument_id)
        if instr is None:
            # Don't shadow the missing-instrument case as a flag refusal —
            # let the downstream code fail loudly with the real reason.
            return
        is_fno = bool(instr.is_fno)
        symbol = instr.symbol
    if not is_fno:
        raise EquityTradingDisabled(
            f"Equity trading disabled (EQUITY_TRADING_ENABLED=false): "
            f"refusing {trade_type} {quantity}×{symbol}"
        )


def _round(v: Decimal) -> Decimal:
    return v.quantize(_TWO, rounding=ROUND_HALF_UP)


class TradingEngine:
    """Executes paper trades with realistic brokerage simulation.

    All monetary arithmetic uses Decimal.
    """

    BROKERAGE_PCT = Decimal("0.0003")   # 0.03%
    BROKERAGE_MAX = Decimal("20")       # ₹20 cap
    STT_DELIVERY_SELL = Decimal("0.001")  # 0.1% on sell
    STT_INTRADAY = Decimal("0.00025")     # 0.025% intraday
    TRANSACTION_CHARGE = Decimal("0.0000345")  # 0.00345% NSE
    GST_ON_BROKERAGE = Decimal("0.18")
    STAMP_DUTY = Decimal("0.00015")  # 0.015% on buy

    def __init__(self) -> None:
        self._risk = RiskManager()
        self._notifier = NotificationService()

    def _calc_charges(
        self,
        trade_type: str,
        quantity: int,
        price: Decimal,
        is_intraday: bool = False,
    ) -> tuple[Decimal, Decimal, Decimal]:
        """Return (brokerage, stt, other_charges) all in Decimal."""
        turnover = price * quantity

        brokerage = min(turnover * self.BROKERAGE_PCT, self.BROKERAGE_MAX)
        gst = _round(brokerage * self.GST_ON_BROKERAGE)
        brokerage = _round(brokerage + gst)

        if trade_type == "SELL":
            stt = _round(turnover * (self.STT_INTRADAY if is_intraday else self.STT_DELIVERY_SELL))
        else:
            stt = Decimal("0")

        txn = _round(turnover * self.TRANSACTION_CHARGE)
        stamp = _round(turnover * self.STAMP_DUTY) if trade_type == "BUY" else Decimal("0")
        other = txn + stamp

        return brokerage, stt, other

    async def execute_market_order(
        self,
        portfolio_id: uuid.UUID,
        instrument_id: uuid.UUID,
        trade_type: str,
        quantity: int,
        current_ltp: Decimal,
        signal_id: uuid.UUID | None = None,
        reason: str | None = None,
    ) -> Trade:
        """Execute a market order at current_ltp and persist all state changes atomically."""
        await _refuse_if_equity_disabled(
            instrument_id, trade_type=trade_type, quantity=quantity
        )
        price = _round(current_ltp)
        brokerage, stt, other = self._calc_charges(trade_type, quantity, price)
        charges = brokerage + stt + other
        turnover = price * quantity

        if trade_type == "BUY":
            total_cost = _round(turnover + charges)
            await self._risk.validate_buy(
                str(portfolio_id), str(instrument_id), quantity, price, total_cost
            )
        else:
            total_cost = _round(turnover - charges)
            await self._risk.validate_sell(str(portfolio_id), str(instrument_id), quantity)

        async with session_scope() as session:
            # Create trade record
            trade = Trade(
                portfolio_id=portfolio_id,
                instrument_id=instrument_id,
                signal_id=signal_id,
                trade_type=trade_type,
                order_type="MARKET",
                quantity=quantity,
                price=float(price),
                brokerage=float(brokerage),
                stt=float(stt),
                total_cost=float(total_cost),
                status="open",
                entry_reason=reason,
                executed_at=datetime.now(timezone.utc),
            )
            session.add(trade)

            # Update portfolio cash
            portfolio = await session.get(Portfolio, portfolio_id)
            if portfolio is None:
                raise RiskError(f"Portfolio {portfolio_id} not found")

            cash = Decimal(str(portfolio.current_cash))
            if trade_type == "BUY":
                portfolio.current_cash = float(cash - total_cost)
                portfolio.invested_value = float(
                    Decimal(str(portfolio.invested_value or 0)) + turnover
                )
            else:
                portfolio.current_cash = float(cash + total_cost)
                portfolio.invested_value = float(
                    Decimal(str(portfolio.invested_value or 0)) - turnover
                )

            # Upsert holding
            result = await session.execute(
                select(Holding).where(
                    Holding.portfolio_id == portfolio_id,
                    Holding.instrument_id == instrument_id,
                )
            )
            holding = result.scalar_one_or_none()

            if trade_type == "BUY":
                if holding is None:
                    holding = Holding(
                        portfolio_id=portfolio_id,
                        instrument_id=instrument_id,
                        quantity=quantity,
                        avg_buy_price=float(price),
                        invested_value=float(turnover),
                        first_buy_date=datetime.now(timezone.utc),
                    )
                    session.add(holding)
                else:
                    old_qty = holding.quantity
                    old_inv = Decimal(str(holding.invested_value))
                    new_qty = old_qty + quantity
                    new_inv = old_inv + turnover
                    holding.quantity = new_qty
                    holding.avg_buy_price = float(_round(new_inv / new_qty))
                    holding.invested_value = float(new_inv)
                holding.last_trade_date = datetime.now(timezone.utc)
            else:
                if holding:
                    new_qty = holding.quantity - quantity
                    if new_qty <= 0:
                        await session.delete(holding)
                    else:
                        # proportionally reduce invested_value
                        ratio = Decimal(str(quantity)) / Decimal(str(holding.quantity))
                        holding.quantity = new_qty
                        holding.invested_value = float(
                            Decimal(str(holding.invested_value)) * (1 - ratio)
                        )
                        holding.last_trade_date = datetime.now(timezone.utc)

            # On a SELL, link FIFO to open BUY legs so realized P&L lands on
            # ``trades.pnl`` and the SELL row carries ``status=closed`` once
            # consumed. Without this the trades table cannot answer "how did
            # the portfolio do today" — every leg stays ``open`` and
            # ``pnl_pct`` never populates. Done inline within the same
            # session_scope so the close-links commit atomically with the
            # SELL trade itself.
            if trade_type == "SELL":
                await self._link_sell_to_open_buys(
                    session,
                    portfolio_id=portfolio_id,
                    instrument_id=instrument_id,
                    sell_trade=trade,
                    sell_price=price,
                    sell_qty=quantity,
                )

            await session.flush()
            await session.refresh(trade)

        instr_result = None
        async with session_scope() as session:
            instr_result = await session.get(Instrument, instrument_id)

        symbol = instr_result.symbol if instr_result else str(instrument_id)
        notif_body = (
            f"{trade_type} {quantity} {symbol} @ ₹{price:,.2f} — "
            f"₹{turnover:,.2f} turnover (charges: ₹{charges:,.2f})"
        )
        await self._notifier.create(
            type_="trade_executed",
            title=f"Trade: {trade_type} {symbol}",
            body=notif_body,
            priority="medium",
            instrument_id=instrument_id,
        )

        logger.info(f"trade executed: {trade_type} {quantity}×{symbol} @ ₹{price}")
        return trade

    async def _link_sell_to_open_buys(
        self,
        session,
        *,
        portfolio_id: uuid.UUID,
        instrument_id: uuid.UUID,
        sell_trade: Trade,
        sell_price: Decimal,
        sell_qty: int,
    ) -> None:
        """FIFO-link a SELL leg to its open BUY legs and close them.

        Downstream consumer note (audited 2026-05-06):
          - ``pnl_aggregator._equity_bucket_rollup`` iterates all trade rows
            for a day and aggregates BUYs by qty + cost. Partial-consume
            creates a closed slice + reduces the surviving open row by the
            same proportion, so totals are preserved.
          - ``equity_strategist._enrich_holding`` queries the still-open
            BUY for ``entry_reason``; the surviving partial keeps
            status='open' so this still returns the opening leg.
          - No API route filters trades by ``status`` directly.
        Reaudit if a new caller relies on ``status='open'`` to enumerate
        all historical entry legs.

        Walks through open BUY trades for ``(portfolio, instrument)`` in
        execution order, consuming up to ``sell_qty`` shares. Each consumed
        BUY row has ``status='closed'``, ``closed_at``, ``closing_trade_id``
        set; ``pnl`` is realized P&L on the consumed slice net of both
        legs' proportional brokerage+STT. ``holding_days`` is the difference
        between the SELL and BUY ``executed_at`` dates.

        If the open BUY quantity is greater than the remaining SELL slice,
        the BUY row is *partially* consumed: a new closed Trade row carries
        the consumed slice's P&L and the original BUY row is reduced by
        that slice. This keeps `holdings` reconciled with leg-level history.
        """
        sell_id = sell_trade.id
        sell_at = sell_trade.executed_at
        # Spread the SELL leg's costs proportionally over consumed quantity.
        sell_cost_per_share = (
            (Decimal(str(sell_trade.brokerage or 0))
             + Decimal(str(sell_trade.stt or 0)))
            / Decimal(max(sell_qty, 1))
        )

        result = await session.execute(
            select(Trade)
            .where(
                Trade.portfolio_id == portfolio_id,
                Trade.instrument_id == instrument_id,
                Trade.trade_type == "BUY",
                Trade.status == "open",
            )
            .order_by(Trade.executed_at.asc())
        )
        open_buys: list[Trade] = list(result.scalars().all())

        remaining = sell_qty
        for buy in open_buys:
            if remaining <= 0:
                break
            buy_qty = int(buy.quantity)
            consume = min(buy_qty, remaining)
            buy_price = Decimal(str(buy.price))
            buy_cost_per_share = (
                (Decimal(str(buy.brokerage or 0))
                 + Decimal(str(buy.stt or 0)))
                / Decimal(max(buy_qty, 1))
            )

            # Realized P&L on the consumed slice, net of both legs' costs.
            slice_pnl = _round(
                (sell_price - buy_price) * Decimal(consume)
                - (buy_cost_per_share + sell_cost_per_share) * Decimal(consume)
            )
            slice_basis = buy_price * Decimal(consume)
            slice_pnl_pct = (
                _round(slice_pnl / slice_basis * Decimal(100))
                if slice_basis > 0 else None
            )
            holding_days = (
                (sell_at.date() - buy.executed_at.date()).days
                if sell_at and buy.executed_at else 0
            )

            if consume == buy_qty:
                # Full consumption — close the BUY row in place.
                buy.status = "closed"
                buy.closed_at = sell_at
                buy.closing_trade_id = sell_id
                buy.pnl = float(slice_pnl)
                buy.pnl_pct = (
                    float(slice_pnl_pct) if slice_pnl_pct is not None else None
                )
                buy.holding_days = holding_days
            else:
                # Partial consumption — split the BUY row. Costs on the
                # consumed slice are split proportionally between brokerage
                # and STT to preserve historical breakdown (matters for tax
                # reporting; P&L itself is unaffected since both legs feed
                # ``slice_pnl`` via the per-share cost combined upstream).
                buy_brokerage = Decimal(str(buy.brokerage or 0))
                buy_stt = Decimal(str(buy.stt or 0))
                slice_share = Decimal(consume) / Decimal(buy_qty)
                slice_brokerage = buy_brokerage * slice_share
                slice_stt = buy_stt * slice_share
                consumed_slice = Trade(
                    portfolio_id=portfolio_id,
                    instrument_id=instrument_id,
                    signal_id=buy.signal_id,
                    trade_type="BUY",
                    order_type=buy.order_type,
                    quantity=consume,
                    price=float(buy_price),
                    brokerage=float(slice_brokerage),
                    stt=float(slice_stt),
                    total_cost=float(
                        buy_price * Decimal(consume)
                        + slice_brokerage + slice_stt
                    ),
                    status="closed",
                    closing_trade_id=sell_id,
                    pnl=float(slice_pnl),
                    pnl_pct=float(slice_pnl_pct) if slice_pnl_pct is not None else None,
                    holding_days=holding_days,
                    entry_reason=buy.entry_reason,
                    executed_at=buy.executed_at,
                    closed_at=sell_at,
                )
                session.add(consumed_slice)
                buy.quantity = buy_qty - consume
                # Reduce the surviving open row's cost and turnover proportionally.
                ratio = (Decimal(buy_qty - consume) / Decimal(buy_qty))
                buy.brokerage = float(Decimal(str(buy.brokerage or 0)) * ratio)
                buy.stt = float(Decimal(str(buy.stt or 0)) * ratio)
                buy.total_cost = float(Decimal(str(buy.total_cost or 0)) * ratio)

            remaining -= consume

        if remaining > 0:
            # Sold more than we had open — likely from a manual reconciliation
            # gap. Log loudly so the operator can investigate; do not fail
            # the trade since the SELL is already executed and persisted.
            logger.warning(
                f"engine: SELL {sell_qty} {instrument_id} consumed only "
                f"{sell_qty - remaining} from open BUYs; {remaining} unmatched"
            )
