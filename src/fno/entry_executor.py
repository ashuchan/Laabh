"""Phase 4 entry executor — turns Phase 3 PROCEED candidates into paper trades.

Wire-up between `entry_engine.propose_entries()` (which picks strategy + legs)
and the persistence/notification layer:

  1. Skip candidates that already have an FNOSignal today (idempotent).
  2. For each new proposal, lookup the latest live chain to get bid/ask.
  3. Use sizer.compute_lots to choose lot count from capital + risk budget.
  4. Use fill_simulator to compute realistic fill price.
  5. Insert FNOSignal row with status='paper_filled', legs JSON, premiums.
  6. Send the existing format_entry_alert Telegram via the gateway.

This is paper-only — there is NO call to a real broker. The FNOSignal row
becomes the source-of-truth for Phase 4 management ticks (stop / target /
hard exit).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from loguru import logger
from sqlalchemy import func, select

from src.config import get_settings
from src.db import session_scope
from src.fno.entry_engine import EntryProposal, propose_entries
from src.fno.execution.fill_simulator import simulate_fill
from src.fno.execution.sizer import compute_lots
from src.fno.notifications import format_entry_alert
from src.fno.strategies.base import Leg
from src.models.fno_chain import OptionsChain
from src.models.fno_signal import FNOSignal
from src.models.instrument import Instrument
from src.services.side_effect_gateway import get_gateway

# Default lot sizes — used when the instrument doesn't carry one.
# These are coarse fallbacks; production should read from instrument master.
_DEFAULT_LOT_SIZE = {
    "NIFTY": 50,
    "BANKNIFTY": 25,
    "FINNIFTY": 40,
    "MIDCPNIFTY": 75,
    "NIFTYNXT50": 25,
}
_DEFAULT_EQUITY_LOT = 500  # NSE F&O equity median lot size


def _lot_size_for(symbol: str) -> int:
    return _DEFAULT_LOT_SIZE.get(symbol.upper(), _DEFAULT_EQUITY_LOT)


async def _latest_bid_ask(
    session,
    instrument_id,
    expiry_date: date,
    strike: Decimal,
    option_type: str,
) -> tuple[Decimal | None, Decimal | None]:
    """Return (bid, ask) from the most recent live options_chain row."""
    snap_subq = (
        select(func.max(OptionsChain.snapshot_at))
        .where(
            OptionsChain.instrument_id == instrument_id,
            OptionsChain.source == "dhan",
        )
        .scalar_subquery()
    )
    row = await session.execute(
        select(OptionsChain.bid_price, OptionsChain.ask_price, OptionsChain.ltp)
        .where(
            OptionsChain.instrument_id == instrument_id,
            OptionsChain.snapshot_at == snap_subq,
            OptionsChain.expiry_date == expiry_date,
            OptionsChain.strike_price == strike,
            OptionsChain.option_type == option_type,
        )
        .limit(1)
    )
    r = row.one_or_none()
    if r is None:
        return None, None
    bid = Decimal(str(r.bid_price)) if r.bid_price is not None else None
    ask = Decimal(str(r.ask_price)) if r.ask_price is not None else None
    if bid is None and ask is None and r.ltp is not None:
        # Fallback: synthesize a tight 0.25% half-spread from LTP.
        ltp = Decimal(str(r.ltp))
        bid = (ltp * Decimal("0.9975")).quantize(Decimal("0.01"))
        ask = (ltp * Decimal("1.0025")).quantize(Decimal("0.01"))
    return bid, ask


async def _already_entered_today(
    session, underlying_id, run_date: date
) -> bool:
    res = await session.execute(
        select(func.count(FNOSignal.id)).where(
            FNOSignal.underlying_id == underlying_id,
            func.date(FNOSignal.proposed_at) == run_date,
            FNOSignal.dryrun_run_id.is_(None),
        )
    )
    return (res.scalar() or 0) > 0


async def _enter_one(proposal: EntryProposal, run_date: date) -> bool:
    """Open one paper position from a proposal. Returns True if newly entered."""
    settings = get_settings()
    cfg_capital = Decimal(str(getattr(settings, "default_capital", 1_000_000)))
    lot_size = _lot_size_for(proposal.symbol)

    async with session_scope() as session:
        if await _already_entered_today(
            session, uuid.UUID(proposal.instrument_id), run_date
        ):
            logger.info(
                f"entry_executor: {proposal.symbol} already has an FNOSignal "
                f"today — skipping"
            )
            return False

    # Compute fills for each leg using the latest live chain
    fills = []
    total_net_cost = Decimal("0")
    async with session_scope() as session:
        for leg in proposal.legs:
            bid, ask = await _latest_bid_ask(
                session,
                uuid.UUID(proposal.instrument_id),
                proposal.expiry_date,
                Decimal(str(leg.strike)),
                leg.option_type,
            )
            if bid is None or ask is None:
                logger.warning(
                    f"entry_executor: {proposal.symbol} leg {leg.option_type} "
                    f"{leg.strike} has no bid/ask — skipping entry"
                )
                return False
            # Sizer is leg-1 only; multi-leg uses lots from leg-1 for now
            lots = (
                compute_lots(
                    portfolio_capital=cfg_capital,
                    max_risk_per_lot=proposal.max_risk * lot_size,
                    lot_size=lot_size,
                    atm_premium=proposal.entry_premium,
                    risk_per_trade_pct=settings.fno_sizing_risk_per_trade_pct,
                    max_position_pct=settings.fno_sizing_max_position_pct,
                    vix_regime="neutral",
                )
                if not fills
                else fills[0].quantity_lots
            )
            if lots <= 0:
                logger.warning(
                    f"entry_executor: {proposal.symbol} sizer returned 0 lots "
                    f"(max_risk={proposal.max_risk}) — skipping"
                )
                return False
            fill = simulate_fill(
                action=leg.action,
                bid=bid,
                ask=ask,
                quantity_lots=lots,
                lot_size=lot_size,
            )
            fills.append(fill)
            total_net_cost += fill.net_cost

    # Build FNOSignal + send entry alert
    signal_id = uuid.uuid4()
    legs_json = [
        {
            "option_type": leg.option_type,
            "strike": str(leg.strike),
            "action": leg.action,
            "quantity_lots": fill.quantity_lots,
            "fill_price": str(fill.fill_price),
        }
        for leg, fill in zip(proposal.legs, fills)
    ]

    async with session_scope() as session:
        sig = FNOSignal(
            id=signal_id,
            underlying_id=uuid.UUID(proposal.instrument_id),
            candidate_id=uuid.UUID(proposal.candidate_id) if proposal.candidate_id else None,
            strategy_type=proposal.strategy_name,
            expiry_date=proposal.expiry_date,
            legs=legs_json,
            entry_premium_net=total_net_cost,
            target_premium_net=(total_net_cost * Decimal("1.30")).quantize(Decimal("0.01")),
            stop_premium_net=(total_net_cost * Decimal("0.70")).quantize(Decimal("0.01")),
            max_loss=proposal.max_risk * fills[0].quantity_lots * lot_size,
            max_profit=proposal.max_reward * fills[0].quantity_lots * lot_size
                if proposal.max_reward != Decimal("inf")
                else None,
            iv_regime_at_entry=None,
            vix_at_entry=None,
            status="paper_filled",
            proposed_at=datetime.now(tz=timezone.utc),
            filled_at=datetime.now(tz=timezone.utc),
            ranker_version=settings.fno_ranker_version,
        )
        session.add(sig)

    # Send the entry alert via the side-effect gateway (LiveGateway → Telegram)
    leg0 = proposal.legs[0]
    fill0 = fills[0]
    msg = format_entry_alert(
        symbol=proposal.symbol,
        strategy_name=proposal.strategy_name,
        fill_price=fill0.fill_price,
        strike=Decimal(str(leg0.strike)),
        option_type=leg0.option_type,
        lots=fill0.quantity_lots,
        stop_price=proposal.stop_premium or fill0.fill_price * Decimal("0.70"),
        target_price=proposal.target_premium or fill0.fill_price * Decimal("1.30"),
    )
    try:
        await get_gateway().send_telegram(msg)
    except Exception as exc:
        logger.warning(f"entry_executor: telegram send failed for {proposal.symbol}: {exc}")

    logger.info(
        f"entry_executor: ENTERED {proposal.symbol} {proposal.strategy_name} "
        f"{leg0.action} {leg0.option_type} {leg0.strike} × {fill0.quantity_lots} lots "
        f"@ ₹{fill0.fill_price}"
    )
    return True


async def auto_enter(run_date: date | None = None) -> dict:
    """Open paper positions for every Phase 3 PROCEED that doesn't already
    have an FNOSignal today. Idempotent — safe to re-run.
    """
    if run_date is None:
        run_date = date.today()

    proposals = await propose_entries(run_date)
    if not proposals:
        logger.info("entry_executor: no proposals to enter")
        return {"proposed": 0, "entered": 0, "skipped": 0}

    entered = skipped = 0
    for prop in proposals:
        try:
            if await _enter_one(prop, run_date):
                entered += 1
            else:
                skipped += 1
        except Exception as exc:
            logger.warning(f"entry_executor: {prop.symbol} entry failed: {exc}")
            skipped += 1

    logger.info(
        f"entry_executor: done — entered={entered} skipped={skipped} "
        f"of {len(proposals)} proposals"
    )
    return {"proposed": len(proposals), "entered": entered, "skipped": skipped}
