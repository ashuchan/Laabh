"""Master 3-min loop — ties all layers together.

run_loop(portfolio_id) is the entry point called by the scheduler when
LAABH_INTRADAY_MODE=quant.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from loguru import logger

from src.config import get_settings
from src.quant import feature_store
from src.quant.bandit.lints import build_context
from src.quant.circuit_breaker import CircuitState
from src.quant.exits import OpenPosition, should_close
from src.quant import persistence
from src.quant import reports
from src.quant.sizer import compute_lots
from src.quant.universe import load_universe


def _make_arm_id(symbol: str, primitive_name: str) -> str:
    return f"{symbol}_{primitive_name}"


def _load_primitives(enabled: list[str]):
    """Import and instantiate enabled primitive classes."""
    from src.quant.primitives.orb import ORBPrimitive
    from src.quant.primitives.vwap_revert import VWAPRevertPrimitive
    from src.quant.primitives.ofi import OFIPrimitive
    from src.quant.primitives.vol_breakout import VolBreakoutPrimitive
    from src.quant.primitives.momentum import MomentumPrimitive
    from src.quant.primitives.index_revert import IndexRevertPrimitive

    registry = {
        "orb": ORBPrimitive,
        "vwap_revert": VWAPRevertPrimitive,
        "ofi": OFIPrimitive,
        "vol_breakout": VolBreakoutPrimitive,
        "momentum": MomentumPrimitive,
        "index_revert": IndexRevertPrimitive,
    }
    return [registry[name]() for name in enabled if name in registry]


async def run_loop(
    portfolio_id: uuid.UUID,
    *,
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> None:
    """3-min orchestration loop for quant mode.

    Args:
        portfolio_id: The portfolio being traded.
        as_of: Override "now" for replay/backtest. None → live.
        dryrun_run_id: Dry-run correlation ID; when set, no real orders placed.
    """
    settings = get_settings()
    today = (as_of or datetime.now(timezone.utc)).date()

    logger.info(f"[QUANT] Starting orchestrator loop for {today} (portfolio={portfolio_id})")

    # --- Bootstrap ---
    universe = await load_universe(today, as_of=as_of, dryrun_run_id=dryrun_run_id)
    if not universe:
        logger.warning("[QUANT] Empty universe — aborting orchestrator loop")
        return

    underlying_map: dict[str, uuid.UUID] = {u["symbol"]: u["id"] for u in universe}
    all_arms = [
        _make_arm_id(u["symbol"], p)
        for u in universe
        for p in settings.quant_primitives_list
    ]

    selector = await persistence.load_morning(
        portfolio_id, today, all_arms, underlying_map,
        as_of=as_of, dryrun_run_id=dryrun_run_id,
    )

    primitives = _load_primitives(settings.quant_primitives_list)
    history: dict[str, list] = {u["symbol"]: [] for u in universe}  # symbol → [FeatureBundle]
    open_positions: list[OpenPosition] = []

    starting_nav = await _get_nav(portfolio_id)
    circuit = CircuitState(starting_nav=starting_nav)

    await _init_day_state(portfolio_id, today, starting_nav, universe, settings)

    import pytz
    ist = pytz.timezone("Asia/Kolkata")

    # --- Main loop ---
    while True:
        now_utc = as_of or datetime.now(timezone.utc)
        now_ist = now_utc.astimezone(ist)

        if now_ist.time() >= settings.laabh_quant_hard_exit_time:
            break

        tick_start = now_utc
        logger.debug(f"[QUANT] tick at {now_ist.strftime('%H:%M:%S')} IST")

        # 1. Refresh features for each underlying
        features_map: dict[str, Any] = {}
        for u in universe:
            bundle = await feature_store.get(u["id"], now_utc)
            if bundle:
                features_map[u["symbol"]] = bundle
                history[u["symbol"]].append(bundle)

        # 2. Compute signals from each enabled primitive × each underlying
        signals: list[tuple[str, str, Any]] = []  # (arm_id, symbol, signal)
        for u in universe:
            symbol = u["symbol"]
            bundle = features_map.get(symbol)
            if bundle is None:
                continue
            hist = history[symbol][:-1]  # exclude current bundle
            for prim in primitives:
                sig = prim.compute_signal(bundle, hist)
                if sig and abs(sig.strength) >= settings.laabh_quant_min_signal_strength:
                    arm_id = _make_arm_id(symbol, prim.name)
                    signals.append((arm_id, symbol, sig))

        # 3. Manage existing positions
        current_nav = await _get_nav(portfolio_id)
        circuit.check_and_fire(current_nav, now_utc)

        for pos in list(open_positions):
            symbol = _symbol_from_arm(pos.arm_id)
            bundle = features_map.get(symbol)
            if bundle is None:
                continue
            current_premium = await _get_premium(pos)
            arm_signals = [(arm_id, sig) for arm_id, _, sig in signals if arm_id == pos.arm_id]
            close, reason = should_close(
                pos, current_premium, bundle.realized_vol_3min, now_utc, arm_signals
            )
            if close:
                pnl = await _close_position(pos, current_premium, reason, portfolio_id)
                open_positions.remove(pos)
                reward = float(pnl) / float(pos.entry_premium_net) if pos.entry_premium_net else 0.0
                selector.update(pos.arm_id, reward)
                if pnl > 0:
                    circuit.record_win(pos.arm_id)
                else:
                    circuit.record_loss(pos.arm_id, now_utc)

        # 4. Day-level circuit breaker
        if circuit.kill_active:
            logger.info("[QUANT] Kill switch active — skipping new entries this tick")
            await _sleep_tick(settings, tick_start, as_of)
            continue

        # 5. Capacity gate
        if len(open_positions) >= settings.laabh_quant_max_concurrent_positions:
            await _sleep_tick(settings, tick_start, as_of)
            continue

        # 6. First-entry warmup gate
        session_open_ist = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
        minutes_since_open = (now_ist - session_open_ist).total_seconds() / 60.0
        if minutes_since_open < settings.laabh_quant_first_entry_after_minutes:
            await _sleep_tick(settings, tick_start, as_of)
            continue

        # 7. Bandit selection
        signalling_arms = [arm_id for arm_id, _, _ in signals]
        active_arms = [
            a for a in signalling_arms
            if not circuit.arm_in_cooloff(a, now_utc)
        ]
        if not active_arms:
            await _sleep_tick(settings, tick_start, as_of)
            continue

        signal_strengths = {arm_id: sig.strength for arm_id, _, sig in signals}
        chosen_arm = selector.select(
            active_arms,
            signal_strengths=signal_strengths,
        )
        if chosen_arm is None:
            await _sleep_tick(settings, tick_start, as_of)
            continue

        # 8. Size and open position
        chosen_entry = next((sig for arm_id, _, sig in signals if arm_id == chosen_arm), None)
        if chosen_entry is None:
            await _sleep_tick(settings, tick_start, as_of)
            continue

        capital = Decimal(str(current_nav))
        max_loss_per_lot = Decimal(str(float(capital) * 0.01))  # rough 1% max loss per lot
        estimated_costs = Decimal("250")  # STT + brokerage estimate
        expected_gross = Decimal(str(float(capital) * 0.02 * chosen_entry.strength))

        lots = compute_lots(
            posterior_mean=selector.posterior_mean(chosen_arm),
            portfolio_capital=capital,
            max_loss_per_lot=max_loss_per_lot,
            estimated_costs=estimated_costs,
            expected_gross_pnl=expected_gross,
            open_exposure=_total_exposure(open_positions),
            lockin_active=circuit.lockin_active,
        )

        if lots > 0:
            pos = await _open_position(
                chosen_arm, chosen_entry, lots, portfolio_id,
                selector.posterior_mean(chosen_arm),
                now_utc,
            )
            if pos:
                open_positions.append(pos)

        await _sleep_tick(settings, tick_start, as_of)

    # --- End of day ---
    logger.info(f"[QUANT] Hard exit time reached — closing {len(open_positions)} positions")
    for pos in list(open_positions):
        premium = await _get_premium(pos)
        await _close_position(pos, premium, "time_stop", portfolio_id)

    final_nav = await _get_nav(portfolio_id)
    await _finalize_day_state(portfolio_id, today, final_nav, starting_nav)
    await persistence.save_eod(
        portfolio_id, today, selector, all_arms, underlying_map
    )
    await reports.generate_eod(portfolio_id, today)
    logger.info(f"[QUANT] Day complete. Final NAV={final_nav:.2f}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _symbol_from_arm(arm_id: str) -> str:
    from src.quant.persistence import _split_arm
    symbol, _ = _split_arm(arm_id)
    return symbol


def _total_exposure(positions: list[OpenPosition]) -> Decimal:
    return sum((p.entry_premium_net for p in positions), Decimal("0"))


async def _get_nav(portfolio_id: uuid.UUID) -> float:
    """Return current_cash + invested_value from portfolios table."""
    from src.db import session_scope
    from sqlalchemy import select
    from src.models.portfolio import Portfolio

    async with session_scope() as session:
        row = await session.get(Portfolio, portfolio_id)
        if row is None:
            return 0.0
        return float(row.current_cash or 0) + float(row.invested_value or 0)


async def _get_premium(pos: OpenPosition) -> Decimal:
    """Return estimated current mid premium (stub — integrates with chain in live mode)."""
    # In live mode this would query options_chain for current ATM mid.
    # For now, return entry_premium_net as a neutral placeholder.
    return pos.entry_premium_net


async def _open_position(
    arm_id: str,
    signal,
    lots: int,
    portfolio_id: uuid.UUID,
    posterior_mean: float,
    now: datetime,
) -> OpenPosition | None:
    """Record a new quant trade in the DB and return an OpenPosition."""
    from src.db import session_scope
    from src.models.quant_trade import QuantTrade
    from src.quant.persistence import _split_arm
    import time as _time

    symbol, primitive_name = _split_arm(arm_id)
    entry_premium = Decimal("100")  # stub — real impl queries live chain

    try:
        async with session_scope() as session:
            trade = QuantTrade(
                portfolio_id=portfolio_id,
                underlying_id=uuid.uuid4(),  # placeholder until universe mapping wired
                primitive_name=primitive_name,
                arm_id=arm_id,
                direction=signal.strategy_class,
                legs={},
                entry_at=now,
                entry_premium_net=entry_premium,
                estimated_costs=Decimal("250"),
                signal_strength_at_entry=signal.strength,
                posterior_mean_at_entry=posterior_mean,
                sampled_mean_at_entry=posterior_mean,
                bandit_seed=int(_time.time()),
                kelly_fraction=0.5,
                lots=lots,
                status="open",
            )
            session.add(trade)
    except Exception as exc:
        logger.error(f"[QUANT] Failed to open position {arm_id}: {exc!r}")
        return None

    pos = OpenPosition(
        arm_id=arm_id,
        underlying_id=symbol,
        direction="bullish" if "call" in signal.strategy_class else "bearish",
        entry_premium_net=entry_premium,
        entry_at=now,
    )
    pos.initial_risk_r = entry_premium * Decimal("0.2")  # 20% of premium
    logger.info(f"[QUANT] Opened {arm_id} × {lots} lots @ {entry_premium}")
    return pos


async def _close_position(
    pos: OpenPosition,
    exit_premium: Decimal,
    reason: str,
    portfolio_id: uuid.UUID,
) -> Decimal:
    """Mark the quant trade closed and return realized P&L."""
    from src.db import session_scope
    from sqlalchemy import select
    from src.models.quant_trade import QuantTrade
    from datetime import datetime, timezone

    pnl = exit_premium - pos.entry_premium_net
    try:
        async with session_scope() as session:
            q = (
                select(QuantTrade)
                .where(QuantTrade.arm_id == pos.arm_id)
                .where(QuantTrade.portfolio_id == portfolio_id)
                .where(QuantTrade.status == "open")
                .order_by(QuantTrade.entry_at.desc())
                .limit(1)
            )
            trade = (await session.execute(q)).scalar_one_or_none()
            if trade:
                trade.exit_at = datetime.now(timezone.utc)
                trade.exit_premium_net = exit_premium
                trade.realized_pnl = pnl
                trade.exit_reason = reason
                trade.status = "closed"
    except Exception as exc:
        logger.error(f"[QUANT] Failed to close position {pos.arm_id}: {exc!r}")

    logger.info(f"[QUANT] Closed {pos.arm_id} → P&L={pnl:.2f} ({reason})")
    return pnl


async def _init_day_state(
    portfolio_id: uuid.UUID,
    today: date,
    starting_nav: float,
    universe: list[dict],
    settings,
) -> None:
    from src.db import session_scope
    from src.models.quant_day_state import QuantDayState

    async with session_scope() as session:
        existing = await session.get(QuantDayState, (portfolio_id, today))
        if existing is None:
            state = QuantDayState(
                portfolio_id=portfolio_id,
                date=today,
                starting_nav=starting_nav,
                universe=[str(u["id"]) for u in universe],
                lockin_target_pct=settings.laabh_quant_lockin_target_pct,
                kill_switch_pct=settings.laabh_quant_kill_switch_dd_pct,
                bandit_algo=settings.laabh_quant_bandit_algo,
                forget_factor=settings.laabh_quant_bandit_forget_factor,
            )
            session.add(state)


async def _finalize_day_state(
    portfolio_id: uuid.UUID,
    today: date,
    final_nav: float,
    starting_nav: float,
) -> None:
    from src.db import session_scope
    from src.models.quant_day_state import QuantDayState
    from sqlalchemy import select, func
    from src.models.quant_trade import QuantTrade

    async with session_scope() as session:
        state = await session.get(QuantDayState, (portfolio_id, today))
        if state:
            state.final_nav = final_nav
            state.pnl_pct = (final_nav - starting_nav) / starting_nav if starting_nav else 0.0

            # Count trades
            q = select(func.count()).where(
                QuantTrade.portfolio_id == portfolio_id,
            )
            state.trade_count = (await session.execute(q)).scalar() or 0


async def _sleep_tick(settings, tick_start: datetime, as_of: datetime | None) -> None:
    if as_of is not None:
        return  # In replay mode, don't sleep
    elapsed = (datetime.now(timezone.utc) - tick_start).total_seconds()
    sleep_sec = max(0.0, settings.laabh_quant_poll_interval_sec - elapsed)
    if sleep_sec > 0:
        await asyncio.sleep(sleep_sec)
