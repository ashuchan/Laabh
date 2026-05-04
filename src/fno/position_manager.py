"""Intraday position manager — drives Phase 4 mark-to-market + lifecycle.

Runs every minute during market hours (09:15-14:30 IST). For every open
FNOSignal:
  1. Look up the latest options_chain row for the leg's strike + option_type.
  2. Compute the current premium (mid of bid/ask, falling back to LTP).
  3. Call intraday_manager.apply_tick to decide hold / stop / target.
  4. On 'stop' or 'target': close the FNOSignal (status, closed_at, final_pnl)
     and emit the corresponding Telegram alert via the side-effect gateway.
  5. Update trailing stop in DB whenever apply_tick changes the position's
     stop_price.
  6. At 14:30 IST: close every still-open position with status='closed_time'
     and a hard-exit Telegram alert.

Position digest: also exposes `format_open_positions_digest()` so morning
brief / ad-hoc messages can include a current snapshot of all live trades
with their MTM P&L.

This module owns no in-memory state — every tick re-reads from `fno_signals`,
which keeps it crash-safe and Phase 4-restartable.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal

from loguru import logger
from sqlalchemy import func, select, update

from src.config import get_settings
from src.db import session_scope
from src.fno.intraday_manager import OpenPosition, apply_tick, update_trailing_stop
from src.fno.notifications import (
    _escape,
    format_hard_exit_alert,
    format_stop_alert,
    format_target_alert,
)
from src.models.fno_chain import OptionsChain
from src.models.fno_signal import FNOSignal
from src.models.instrument import Instrument
from src.services.side_effect_gateway import get_gateway


_LIVE_STATUSES = ("paper_filled", "active", "scaled_out_50")


# Lot-size table mirrors entry_executor; keep in sync.
_DEFAULT_LOT_SIZE = {
    "NIFTY": 50, "BANKNIFTY": 25, "FINNIFTY": 40,
    "MIDCPNIFTY": 75, "NIFTYNXT50": 25,
}
_DEFAULT_EQUITY_LOT = 500


def _lot_size_for(symbol: str) -> int:
    return _DEFAULT_LOT_SIZE.get(symbol.upper(), _DEFAULT_EQUITY_LOT)


async def _latest_premium(
    session,
    instrument_id,
    expiry_date: date,
    strike: Decimal,
    option_type: str,
) -> Decimal | None:
    """Return current mid (preferred) or LTP for one leg from latest chain row."""
    snap_subq = (
        select(func.max(OptionsChain.snapshot_at))
        .where(OptionsChain.instrument_id == instrument_id)
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
        return None
    if r.bid_price is not None and r.ask_price is not None:
        return ((Decimal(str(r.bid_price)) + Decimal(str(r.ask_price))) / 2).quantize(
            Decimal("0.01")
        )
    if r.ltp is not None:
        return Decimal(str(r.ltp))
    return None


def _signal_to_open_position(sig: FNOSignal, symbol: str) -> OpenPosition | None:
    """Reconstruct an OpenPosition view from a persisted FNOSignal row.

    For multi-leg strategies we treat leg-1 as the directional leg for tick
    decisions; the simpler single-leg strategies (long_call, long_put) fit
    this exactly.
    """
    legs = sig.legs or []
    if not legs:
        return None
    leg0 = legs[0]
    try:
        strike = Decimal(str(leg0.get("strike")))
        opt_type = str(leg0.get("option_type") or "").upper()
        fill = Decimal(str(leg0.get("fill_price") or 0))
        lots = int(leg0.get("quantity_lots") or 1)
    except Exception:
        return None
    if opt_type not in ("CE", "PE") or fill <= 0:
        return None
    lot_size = _lot_size_for(symbol)

    # `target_premium_net` and `stop_premium_net` on FNOSignal are stored as
    # *net total* rupees (price × lots × lot_size + fees), but apply_tick
    # compares against per-share premium. Derive stop/target as ±30% of the
    # per-share leg-1 fill so the units match. (For multi-leg strategies the
    # net P&L sense still holds since we manage off leg-1's directional move.)
    target = (fill * Decimal("1.30")).quantize(Decimal("0.01"))
    stop = (fill * Decimal("0.70")).quantize(Decimal("0.01"))

    return OpenPosition(
        instrument_id=str(sig.underlying_id),
        symbol=symbol,
        strategy_name=sig.strategy_type,
        option_type=opt_type,
        strike=strike,
        entry_price=fill,
        stop_price=stop,
        target_price=target,
        lots=lots,
        lot_size=lot_size,
        entered_at=sig.filled_at or sig.proposed_at,
    )


def _compute_pnl(entry: Decimal, exit_p: Decimal, lots: int, lot_size: int, side: str) -> Decimal:
    """Net P&L for closing one option leg. side='BUY' means we're long → P&L = (exit-entry)*qty."""
    qty = Decimal(lots * lot_size)
    if side.upper() == "BUY":
        return ((exit_p - entry) * qty).quantize(Decimal("0.01"))
    return ((entry - exit_p) * qty).quantize(Decimal("0.01"))


async def _close_signal(
    sig_id, *, status: str, exit_price: Decimal, final_pnl: Decimal, new_stop: Decimal | None
) -> None:
    async with session_scope() as session:
        values = {
            "status": status,
            "closed_at": datetime.now(tz=timezone.utc),
            "final_pnl": final_pnl,
        }
        if new_stop is not None:
            values["stop_premium_net"] = new_stop
        await session.execute(
            update(FNOSignal).where(FNOSignal.id == sig_id).values(**values)
        )


async def _update_trailing_stop_in_db(sig_id, new_stop: Decimal) -> None:
    async with session_scope() as session:
        await session.execute(
            update(FNOSignal)
            .where(FNOSignal.id == sig_id)
            .values(stop_premium_net=new_stop)
        )


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

async def manage_tick() -> dict:
    """One management tick — runs every minute via APScheduler.

    Mark-to-market every open FNOSignal; close ones that hit stop/target;
    update trailing stops; emit Telegram alerts on close.
    """
    cfg = get_settings()
    closed = 0
    trailing = 0
    held = 0
    skipped = 0

    async with session_scope() as session:
        rows = await session.execute(
            select(FNOSignal, Instrument.symbol)
            .join(Instrument, Instrument.id == FNOSignal.underlying_id)
            .where(
                FNOSignal.status.in_(_LIVE_STATUSES),
                FNOSignal.dryrun_run_id.is_(None),
            )
        )
        signals = list(rows.all())

    if not signals:
        return {"open": 0, "closed": 0, "trailing": 0, "skipped": 0}

    for sig, symbol in signals:
        pos = _signal_to_open_position(sig, symbol)
        if pos is None:
            skipped += 1
            continue

        async with session_scope() as session:
            current = await _latest_premium(
                session,
                sig.underlying_id,
                sig.expiry_date,
                pos.strike,
                pos.option_type,
            )
        if current is None:
            skipped += 1
            continue

        old_stop = pos.stop_price
        action = apply_tick(
            pos, current,
            scale_out_pct=cfg.fno_phase4_scale_out_at_pct_gain,
            trailing_stop_pct=cfg.fno_phase4_trailing_stop_from_peak_pct,
        )

        # Trailing stop may have moved during apply_tick; persist if so.
        if pos.stop_price != old_stop:
            await _update_trailing_stop_in_db(sig.id, pos.stop_price)
            trailing += 1
            logger.info(
                f"position_manager: {symbol} trailing stop {old_stop} -> {pos.stop_price} "
                f"(price={current}, peak={pos.peak_price})"
            )

        if action == "hold":
            held += 1
            continue

        # Close out: BUY entry → SELL exit (single-leg long premium)
        leg0 = (sig.legs or [{}])[0]
        side = leg0.get("action", "BUY")
        pnl = _compute_pnl(pos.entry_price, current, pos.lots, pos.lot_size, side)
        new_status = "closed_target" if action == "target" else "closed_stop"
        await _close_signal(
            sig.id,
            status=new_status,
            exit_price=current,
            final_pnl=pnl,
            new_stop=pos.stop_price,
        )
        closed += 1

        msg = (
            format_target_alert(symbol, current, pos.entry_price, pnl)
            if action == "target"
            else format_stop_alert(symbol, current, pos.entry_price, pnl)
        )
        try:
            await get_gateway().send_telegram(msg)
        except Exception as exc:
            logger.warning(f"position_manager: telegram failed for {symbol}: {exc}")

        logger.info(
            f"position_manager: {symbol} {new_status.upper()} @ ₹{current} "
            f"P&L=₹{pnl} (entry=₹{pos.entry_price})"
        )

    return {
        "open": len(signals),
        "closed": closed,
        "trailing": trailing,
        "held": held,
        "skipped": skipped,
    }


async def hard_exit_all() -> dict:
    """14:30 IST hard exit — close every still-open paper position."""
    closed = 0
    skipped = 0

    async with session_scope() as session:
        rows = await session.execute(
            select(FNOSignal, Instrument.symbol)
            .join(Instrument, Instrument.id == FNOSignal.underlying_id)
            .where(
                FNOSignal.status.in_(_LIVE_STATUSES),
                FNOSignal.dryrun_run_id.is_(None),
            )
        )
        signals = list(rows.all())

    for sig, symbol in signals:
        pos = _signal_to_open_position(sig, symbol)
        if pos is None:
            skipped += 1
            continue
        async with session_scope() as session:
            current = await _latest_premium(
                session, sig.underlying_id, sig.expiry_date,
                pos.strike, pos.option_type,
            )
        # Hard-exit even with no fresh chain — fall back to entry price (P&L=0)
        exit_price = current if current is not None else pos.entry_price
        leg0 = (sig.legs or [{}])[0]
        side = leg0.get("action", "BUY")
        pnl = _compute_pnl(pos.entry_price, exit_price, pos.lots, pos.lot_size, side)
        await _close_signal(
            sig.id, status="closed_time",
            exit_price=exit_price, final_pnl=pnl, new_stop=None,
        )
        closed += 1
        try:
            await get_gateway().send_telegram(
                format_hard_exit_alert(symbol, exit_price, pos.entry_price, pnl)
            )
        except Exception as exc:
            logger.warning(f"position_manager: hard-exit telegram failed for {symbol}: {exc}")
        logger.info(
            f"position_manager: HARD-EXIT {symbol} @ ₹{exit_price} P&L=₹{pnl}"
        )

    return {"closed": closed, "skipped": skipped}


# ---------------------------------------------------------------------------
# Status digest — shippable in morning brief / ad-hoc messages
# ---------------------------------------------------------------------------

async def open_positions_summary() -> list[dict]:
    """Return a summary dict per currently-open FNOSignal, with MTM."""
    out: list[dict] = []
    async with session_scope() as session:
        rows = await session.execute(
            select(FNOSignal, Instrument.symbol)
            .join(Instrument, Instrument.id == FNOSignal.underlying_id)
            .where(
                FNOSignal.status.in_(_LIVE_STATUSES),
                FNOSignal.dryrun_run_id.is_(None),
            )
            .order_by(FNOSignal.proposed_at)
        )
        signals = list(rows.all())

    for sig, symbol in signals:
        pos = _signal_to_open_position(sig, symbol)
        if pos is None:
            continue
        async with session_scope() as session:
            current = await _latest_premium(
                session, sig.underlying_id, sig.expiry_date,
                pos.strike, pos.option_type,
            )
        leg0 = (sig.legs or [{}])[0]
        side = leg0.get("action", "BUY")
        mtm = (
            _compute_pnl(pos.entry_price, current, pos.lots, pos.lot_size, side)
            if current is not None
            else Decimal("0")
        )
        out.append({
            "symbol": symbol,
            "strategy": sig.strategy_type,
            "option_type": pos.option_type,
            "strike": pos.strike,
            "entry": pos.entry_price,
            "current": current,
            "stop": pos.stop_price,
            "target": pos.target_price,
            "lots": pos.lots,
            "mtm": mtm,
            "status": sig.status,
        })
    return out


def format_position_digest(positions: list[dict]) -> str:
    """Telegram-friendly digest of all open positions with MTM."""
    if not positions:
        return "📊 *Open Positions:* none"

    total_mtm = sum((p["mtm"] for p in positions), Decimal("0"))
    sign = "\\+" if total_mtm >= 0 else ""
    lines = [f"📊 *Open Positions \\({len(positions)}\\)*  Total MTM: {sign}₹{_escape(str(total_mtm))}"]
    for p in positions:
        cur = f"₹{p['current']}" if p["current"] is not None else "n/a"
        mtm_sign = "\\+" if p["mtm"] >= 0 else ""
        lines.append(
            f"\n*{_escape(p['symbol'])}*  {_escape(p['option_type'])} {_escape(str(p['strike']))} "
            f"× {p['lots']}L\n"
            f"  Entry: ₹{_escape(str(p['entry']))} → Now: {_escape(cur)}  "
            f"MTM: {mtm_sign}₹{_escape(str(p['mtm']))}\n"
            f"  Stop: ₹{_escape(str(p['stop']))} \\| Target: ₹{_escape(str(p['target']))}"
        )
    return "".join(lines)


async def send_position_digest() -> int:
    """Compose and send the open-positions digest to Telegram. Returns count."""
    positions = await open_positions_summary()
    msg = format_position_digest(positions)
    try:
        await get_gateway().send_telegram(msg)
    except Exception as exc:
        logger.warning(f"position_manager: digest send failed: {exc}")
    return len(positions)
