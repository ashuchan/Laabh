"""Master 3-min loop — ties all layers together.

run_loop(portfolio_id) is the entry point called by the scheduler when
LAABH_INTRADAY_MODE=quant.
"""
from __future__ import annotations

import asyncio
import math
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import numpy as np
import pytz
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

_IST = pytz.timezone("Asia/Kolkata")
# Total tradeable minutes per session: 09:15–14:30 = 315 min
_SESSION_MINUTES = 315.0


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


def _build_tick_context(
    *,
    features_map: dict[str, Any],
    minutes_since_open: float,
    day_running_pnl_pct: float,
) -> np.ndarray:
    """Build the 5-dim LinTS context vector for the current tick.

    Falls back gracefully when VIX or vol data is unavailable (e.g. warmup).
    """
    # Use VIX from any available bundle (all underlyings share the same VIX value)
    vix = 15.0
    rv30_pctile = 0.5
    for bundle in features_map.values():
        vix = bundle.vix_value
        # Normalise vol percentile: rv30min clamped to [0, 1] using a rough 0–1 annualised vol scale
        rv30_pctile = min(1.0, max(0.0, bundle.realized_vol_30min / 1.0))
        break

    return build_context(
        vix_value=vix,
        time_of_day_pct=min(1.0, max(0.0, minutes_since_open / _SESSION_MINUTES)),
        day_running_pnl_pct=day_running_pnl_pct,
        nifty_5d_return=0.0,   # wired in when macro data is available
        realized_vol_30min_pctile=rv30_pctile,
    )


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

    # Pre-compute arm → (symbol, primitive_name) to avoid per-tick string splitting
    arm_meta: dict[str, tuple[str, str]] = {
        _make_arm_id(u["symbol"], p): (u["symbol"], p)
        for u in universe
        for p in settings.quant_primitives_list
    }

    selector = await persistence.load_morning(
        portfolio_id, today, all_arms, underlying_map,
        as_of=as_of, dryrun_run_id=dryrun_run_id,
    )

    primitives = _load_primitives(settings.quant_primitives_list)
    max_history = max((p.warmup_minutes for p in primitives), default=30) + 5
    # ceil so a 35-min warmup (not multiple of 3) keeps enough bars
    max_history_bars = math.ceil(max_history / 3) + 2
    history: dict[str, list] = {u["symbol"]: [] for u in universe}

    open_positions: list[OpenPosition] = []

    starting_nav = await _get_nav(portfolio_id)
    circuit = CircuitState(
        starting_nav=starting_nav,
        lockin_target_pct=settings.laabh_quant_lockin_target_pct,
        kill_switch_dd_pct=settings.laabh_quant_kill_switch_dd_pct,
        cooloff_consecutive_losses=settings.laabh_quant_cooloff_consecutive_losses,
        cooloff_minutes=settings.laabh_quant_cooloff_minutes,
    )

    await _init_day_state(portfolio_id, today, starting_nav, universe, settings)

    # Compute session open once — 09:15 IST
    _session_open_ist_time = _IST.localize(
        datetime(today.year, today.month, today.day, 9, 15, 0)
    )

    replay_mode = as_of is not None
    current_time = as_of if replay_mode else datetime.now(timezone.utc)
    poll_delta = timedelta(seconds=settings.laabh_quant_poll_interval_sec)
    # Keep last features_map for EOD close; initialise empty to avoid NameError
    features_map: dict[str, Any] = {}

    # --- Main loop ---
    while True:
        if not replay_mode:
            current_time = datetime.now(timezone.utc)

        now_ist = current_time.astimezone(_IST)

        if now_ist.time() >= settings.laabh_quant_hard_exit_time:
            break

        tick_start = current_time
        logger.debug(f"[QUANT] tick at {now_ist.strftime('%H:%M:%S')} IST")

        # 1. Refresh features for each underlying
        features_map = {}
        for u in universe:
            bundle = await feature_store.get(u["id"], current_time)
            if bundle:
                features_map[u["symbol"]] = bundle
                sym_hist = history[u["symbol"]]
                sym_hist.append(bundle)
                if len(sym_hist) > max_history_bars:
                    del sym_hist[0]

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
        circuit.check_and_fire(current_nav, current_time)
        day_running_pnl_pct = (current_nav - starting_nav) / starting_nav if starting_nav else 0.0

        for pos in list(open_positions):
            symbol = arm_meta.get(pos.arm_id, (pos.arm_id, ""))[0]
            bundle = features_map.get(symbol)
            if bundle is None:
                continue
            current_premium = _get_premium_from_bundle(pos, bundle)
            arm_signals = [(aid, sig) for aid, _, sig in signals if aid == pos.arm_id]
            close, reason = should_close(
                pos, current_premium, bundle.realized_vol_3min, current_time, arm_signals
            )
            if close:
                pnl = await _close_position(pos, current_premium, reason, portfolio_id, current_time)
                open_positions.remove(pos)
                reward = float(pnl) / float(pos.entry_premium_net) if pos.entry_premium_net else 0.0
                selector.update(pos.arm_id, reward)
                if pnl > 0:
                    circuit.record_win(pos.arm_id)
                else:
                    circuit.record_loss(pos.arm_id, current_time)

        # 4. Day-level circuit breaker
        if circuit.kill_active:
            logger.info("[QUANT] Kill switch active — skipping new entries this tick")
            await _sleep_tick(settings, tick_start, replay_mode)
            if replay_mode:
                current_time += poll_delta
            continue

        # 5. Capacity gate
        if len(open_positions) >= settings.laabh_quant_max_concurrent_positions:
            await _sleep_tick(settings, tick_start, replay_mode)
            if replay_mode:
                current_time += poll_delta
            continue

        # 6. First-entry warmup gate
        minutes_since_open = (now_ist - _session_open_ist_time).total_seconds() / 60.0
        if minutes_since_open < settings.laabh_quant_first_entry_after_minutes:
            await _sleep_tick(settings, tick_start, replay_mode)
            if replay_mode:
                current_time += poll_delta
            continue

        # 7. Bandit selection — build context for LinTS
        signalling_arms = [arm_id for arm_id, _, _ in signals]
        active_arms = [
            a for a in signalling_arms
            if not circuit.arm_in_cooloff(a, current_time)
        ]
        if not active_arms:
            await _sleep_tick(settings, tick_start, replay_mode)
            if replay_mode:
                current_time += poll_delta
            continue

        signal_strengths = {arm_id: sig.strength for arm_id, _, sig in signals}
        context = _build_tick_context(
            features_map=features_map,
            minutes_since_open=minutes_since_open,
            day_running_pnl_pct=day_running_pnl_pct,
        )
        chosen_arm = selector.select(
            active_arms,
            context=context,
            signal_strengths=signal_strengths,
        )
        if chosen_arm is None:
            await _sleep_tick(settings, tick_start, replay_mode)
            if replay_mode:
                current_time += poll_delta
            continue

        # 8. Size and open position
        chosen_entry = next((sig for arm_id, _, sig in signals if arm_id == chosen_arm), None)
        if chosen_entry is None:
            await _sleep_tick(settings, tick_start, replay_mode)
            if replay_mode:
                current_time += poll_delta
            continue

        capital = Decimal(str(current_nav))
        max_loss_per_lot = Decimal(str(float(capital) * 0.01))
        estimated_costs = Decimal("250")
        expected_gross = Decimal(str(float(capital) * 0.02 * chosen_entry.strength))

        lots = compute_lots(
            posterior_mean=selector.posterior_mean(chosen_arm),
            portfolio_capital=capital,
            max_loss_per_lot=max_loss_per_lot,
            estimated_costs=estimated_costs,
            expected_gross_pnl=expected_gross,
            open_exposure=_total_exposure(open_positions),
            lockin_active=circuit.lockin_active,
            kelly_fraction=settings.laabh_quant_kelly_fraction,
            max_per_trade_pct=settings.laabh_quant_max_per_trade_pct,
            lockin_size_reduction=settings.laabh_quant_lockin_size_reduction,
            max_total_exposure_pct=settings.laabh_quant_max_total_exposure_pct,
            cost_gate_multiple=settings.laabh_quant_cost_gate_multiple,
        )

        if lots > 0:
            chosen_symbol, chosen_primitive = arm_meta.get(chosen_arm, (chosen_arm, ""))
            chosen_underlying_id = underlying_map.get(chosen_symbol)
            chosen_bundle = features_map.get(chosen_symbol)
            pos = await _open_position(
                arm_id=chosen_arm,
                primitive_name=chosen_primitive,
                signal=chosen_entry,
                lots=lots,
                portfolio_id=portfolio_id,
                posterior_mean=selector.posterior_mean(chosen_arm),
                now=current_time,
                underlying_id=chosen_underlying_id,
                entry_bundle=chosen_bundle,
                kelly_fraction=settings.laabh_quant_kelly_fraction,
            )
            if pos:
                open_positions.append(pos)

        await _sleep_tick(settings, tick_start, replay_mode)
        if replay_mode:
            current_time += poll_delta

    # --- End of day ---
    # features_map holds last tick's data; acceptable for paper-trading EOD close
    # (worst case: position opened on final tick uses entry_premium_net as exit price → P&L=0)
    logger.info(f"[QUANT] Hard exit time reached — closing {len(open_positions)} positions")
    for pos in list(open_positions):
        symbol = arm_meta.get(pos.arm_id, (pos.arm_id, ""))[0]
        bundle = features_map.get(symbol)
        premium = _get_premium_from_bundle(pos, bundle) if bundle else pos.entry_premium_net
        await _close_position(pos, premium, "time_stop", portfolio_id, current_time)

    final_nav = await _get_nav(portfolio_id)
    await _finalize_day_state(portfolio_id, today, final_nav, starting_nav, circuit)
    await persistence.save_eod(
        portfolio_id, today, selector, all_arms, underlying_map
    )
    await reports.generate_eod(portfolio_id, today)
    logger.info(f"[QUANT] Day complete. Final NAV={final_nav:.2f}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _total_exposure(positions: list[OpenPosition]) -> Decimal:
    return sum((p.entry_premium_net for p in positions), Decimal("0"))


def _get_premium_from_bundle(pos: OpenPosition, bundle) -> Decimal:
    """Return current ATM mid premium from feature bundle, falling back to entry."""
    if bundle is not None and bundle.atm_bid and bundle.atm_ask:
        mid = (bundle.atm_bid + bundle.atm_ask) / Decimal("2")
        return mid
    return pos.entry_premium_net


async def _get_nav(portfolio_id: uuid.UUID) -> float:
    """Return current_cash + invested_value from portfolios table."""
    from src.db import session_scope
    from src.models.portfolio import Portfolio

    async with session_scope() as session:
        row = await session.get(Portfolio, portfolio_id)
        if row is None:
            return 0.0
        return float(row.current_cash or 0) + float(row.invested_value or 0)


async def _open_position(
    *,
    arm_id: str,
    primitive_name: str,
    signal,
    lots: int,
    portfolio_id: uuid.UUID,
    posterior_mean: float,
    now: datetime,
    underlying_id: uuid.UUID | None,
    entry_bundle,
    kelly_fraction: float,
) -> OpenPosition | None:
    """Record a new quant trade in the DB and return an OpenPosition."""
    from src.db import session_scope
    from src.models.quant_trade import QuantTrade

    if underlying_id is None:
        logger.error(f"[QUANT] No underlying_id for {arm_id} — skipping open")
        return None

    # Use live ATM mid as entry premium; stub fallback when chain data unavailable
    entry_premium = _get_premium_from_bundle_stub(entry_bundle)
    # Deterministic seed from portfolio + arm + date for reproducibility
    bandit_seed = abs(hash((str(portfolio_id), arm_id, now.date().isoformat()))) % (2**31)

    try:
        async with session_scope() as session:
            trade = QuantTrade(
                portfolio_id=portfolio_id,
                underlying_id=underlying_id,
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
                bandit_seed=bandit_seed,
                kelly_fraction=kelly_fraction,
                lots=lots,
                status="open",
            )
            session.add(trade)
    except Exception as exc:
        logger.error(f"[QUANT] Failed to open position {arm_id}: {exc!r}")
        return None

    pos = OpenPosition(
        arm_id=arm_id,
        underlying_id=str(underlying_id),
        direction="bullish" if "call" in signal.strategy_class else "bearish",
        entry_premium_net=entry_premium,
        entry_at=now,
    )
    pos.initial_risk_r = entry_premium * Decimal("0.2")
    logger.info(f"[QUANT] Opened {arm_id} × {lots} lots @ {entry_premium}")
    return pos


def _get_premium_from_bundle_stub(bundle) -> Decimal:
    """Return ATM mid from bundle, or ₹100 stub when chain data is unavailable."""
    if bundle is not None and bundle.atm_bid and bundle.atm_ask:
        return (bundle.atm_bid + bundle.atm_ask) / Decimal("2")
    return Decimal("100")


async def _close_position(
    pos: OpenPosition,
    exit_premium: Decimal,
    reason: str,
    portfolio_id: uuid.UUID,
    now: datetime,
) -> Decimal:
    """Mark the quant trade closed and return realized P&L."""
    from src.db import session_scope
    from sqlalchemy import select
    from src.models.quant_trade import QuantTrade

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
                trade.exit_at = now  # use simulated time so replay exit_at is correct
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
                universe=[{"id": str(u["id"]), "symbol": u["symbol"]} for u in universe],
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
    circuit: CircuitState,
) -> None:
    from src.db import session_scope
    from src.models.quant_day_state import QuantDayState
    from sqlalchemy import select, func
    from src.models.quant_trade import QuantTrade
    from src.quant.reports import _day_start

    async with session_scope() as session:
        state = await session.get(QuantDayState, (portfolio_id, today))
        if state:
            state.final_nav = final_nav
            state.pnl_pct = (final_nav - starting_nav) / starting_nav if starting_nav else 0.0
            # Persist circuit-breaker fire times so EOD report can read them
            state.lockin_fired_at = circuit.lockin_fired_at
            state.kill_switch_fired_at = circuit.kill_fired_at

            q = select(func.count()).where(
                QuantTrade.portfolio_id == portfolio_id,
                QuantTrade.entry_at >= _day_start(today),
            )
            state.trade_count = (await session.execute(q)).scalar() or 0


async def _sleep_tick(settings, tick_start: datetime, replay_mode: bool) -> None:
    if replay_mode:
        return
    elapsed = (datetime.now(timezone.utc) - tick_start).total_seconds()
    sleep_sec = max(0.0, settings.laabh_quant_poll_interval_sec - elapsed)
    if sleep_sec > 0:
        await asyncio.sleep(sleep_sec)
