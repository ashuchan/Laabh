"""Master 3-min loop — ties all layers together.

``run_loop(portfolio_id)`` is the entry point called by the scheduler when
``LAABH_INTRADAY_MODE=quant``. The same loop also drives backtest replays
when an ``OrchestratorContext`` with backtest-mode dependencies is injected.

The mode-divergent I/O (clock, feature reads, universe selection, trade
ledger) is encapsulated in ``OrchestratorContext`` (see ``src.quant.context``).
When no context is supplied, ``run_loop`` builds the live default — existing
call sites remain unchanged.
"""
from __future__ import annotations

import asyncio
import hashlib
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
from src.quant.context import OrchestratorContext
from src.quant.exits import OpenPosition, should_close
from src.quant import persistence
from src.quant.recorder import (
    CloseTradePayload,
    DayFinalizePayload,
    DayInitPayload,
    OpenTradePayload,
    RecordSignalsPayload,
    SignalLogEntry,
)
from src.quant import reports
from src.quant.sizer import compute_lots

_IST = pytz.timezone("Asia/Kolkata")
# Total tradeable minutes per session: 09:15–14:30 = 315 min
_SESSION_MINUTES = 315.0


def _make_arm_id(symbol: str, primitive_name: str) -> str:
    return f"{symbol}_{primitive_name}"


_LEGACY_DIRECTION_MAP: dict[str, str] = {
    "bullish": "bullish",
    "bearish": "bearish",
    # Pre-fix orchestrator wrote signal.strategy_class into the direction
    # column. Map the strategy classes back to a canonical direction so
    # crash-recovery doesn't silently coerce "long_put" trades to bullish.
    "long_call": "bullish",
    "long_put": "bearish",
    "debit_call_spread": "bullish",
    "credit_put_spread": "bearish",
    "debit_put_spread": "bearish",
    "credit_call_spread": "bullish",
}


def _normalize_direction(value: object) -> str | None:
    """Map a stored direction / strategy-class to "bullish"|"bearish" or None.

    Returns None for unknown / empty values so callers can log + skip rather
    than picking an arbitrary side.
    """
    if not isinstance(value, str):
        return None
    return _LEGACY_DIRECTION_MAP.get(value.strip().lower())


def _replay_bandit_updates(selector, closed_trades) -> None:
    """Replay closed-today trades against *selector* in entry-time order.

    Without this, a mid-session restart loads yesterday's posteriors
    (γ-decayed) and silently drops every reward observed so far today.

    Iterates trades sorted by entry_at and applies
    selector.update(arm_id, (exit-entry)/entry) — the same per-lot return
    ratio the live tick path uses, so n_obs and posterior_mean stay
    consistent. Skips trades with missing exit_premium or zero entry to
    keep the function total.
    """
    for trade in sorted(closed_trades, key=lambda t: t.entry_at):
        entry = trade.entry_premium_net
        exit_ = trade.exit_premium_net
        if exit_ is None or not entry:
            continue
        reward = float(exit_ - entry) / float(entry)
        selector.update(trade.arm_id, reward)


def _seed_for_arm(portfolio_id: uuid.UUID, arm_id: str, day: date) -> int:
    """Reproducible 31-bit seed from (portfolio, arm, date).

    Uses SHA-256 because Python's built-in hash() is salted per-process when
    PYTHONHASHSEED is unset, which silently breaks replay reproducibility.
    """
    key = f"{portfolio_id}|{arm_id}|{day.isoformat()}".encode("utf-8")
    digest = hashlib.sha256(key).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


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


def _slice_bandit_trace(full: dict | None, arm_id: str) -> dict | None:
    """Return a per-arm slice of the full bandit trace.

    The selector emits the whole tournament keyed by arm_id; per-row storage
    keeps each row self-contained (the inspector reconstructs the tournament
    by aggregating rows for the tick — see PR 2). Returns None when the
    arm didn't compete or no trace was captured.
    """
    if not full:
        return None
    arms = full.get("arms") or {}
    if arm_id not in arms:
        return None
    return {
        "algo": full.get("algo"),
        "context_vector": full.get("context_vector"),
        "context_dims": full.get("context_dims"),
        "this_arm": arms[arm_id],
        "n_competitors": full.get("n_competitors"),
    }


async def _emit_signal_log(
    *,
    ctx: OrchestratorContext,
    virtual_time: datetime,
    all_raw_signals: list,
    tick_disposition: dict[str, str],
    strong_arm_set: set[str],
    selector,
    chosen_arm: str | None,
    lots: int | None,
    primitive_traces: dict[str, dict] | None = None,
    bandit_trace_full: dict | None = None,
    sizer_trace_full: dict | None = None,
) -> None:
    """Build SignalLogEntry rows for this tick and hand them to the recorder.

    Best-effort: any failure in the recorder is logged but never propagated —
    diagnostic logging must never break a live tick. The live recorder's
    ``record_signals`` is a no-op so this path is essentially free outside
    of backtest mode.

    Trace plumbing (Decision Inspector PR 1):
      * primitive_traces — per-arm dict, attached to that arm's row
      * bandit_trace_full — sliced per arm; only competing arms get a slice
      * sizer_trace_full — attached only to the chosen arm's row
    """
    if not all_raw_signals:
        return
    primitive_traces = primitive_traces or {}
    entries: list[SignalLogEntry] = []
    for arm_id, symbol, underlying_id, prim_name, sig in all_raw_signals:
        # Default to weak_signal — if no later gate marked this arm, the
        # signal must have failed the strength filter (the only reason a
        # primitive's output can fall through every later branch).
        reason = tick_disposition.get(arm_id, "weak_signal")
        post = (
            float(selector.posterior_mean(arm_id))
            if arm_id in strong_arm_set
            else None
        )
        is_chosen = chosen_arm is not None and arm_id == chosen_arm
        entries.append(
            SignalLogEntry(
                underlying_id=underlying_id,
                symbol=symbol,
                arm_id=arm_id,
                primitive_name=prim_name,
                direction=sig.direction,
                strength=float(sig.strength),
                rejection_reason=reason,
                posterior_mean=post,
                bandit_selected=is_chosen,
                lots_sized=(lots if is_chosen else None),
                primitive_trace=primitive_traces.get(arm_id),
                bandit_trace=_slice_bandit_trace(bandit_trace_full, arm_id),
                sizer_trace=(sizer_trace_full if is_chosen else None),
            )
        )
    try:
        await ctx.recorder.record_signals(
            RecordSignalsPayload(virtual_time=virtual_time, entries=entries)
        )
    except Exception as exc:
        logger.warning(f"[QUANT] _emit_signal_log: skipped tick log due to {exc!r}")


async def run_loop(
    portfolio_id: uuid.UUID,
    *,
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
    ctx: OrchestratorContext | None = None,
) -> None:
    """3-min orchestration loop for quant mode.

    Args:
        portfolio_id: The portfolio being traded.
        as_of: Override "now" for replay/backtest. None → live.
        dryrun_run_id: Dry-run correlation ID; when set, no real orders placed.
        ctx: Injectable I/O bundle — clock, feature getter, universe selector,
            trade recorder. Defaults to live-mode wiring; backtest callers
            (BacktestRunner) pass a backtest-configured context.
    """
    settings = get_settings()
    if ctx is None:
        ctx = OrchestratorContext.live()

    today = (as_of or ctx.clock.now()).date()

    logger.info(
        f"[QUANT] Starting orchestrator loop for {today} "
        f"(portfolio={portfolio_id}, mode={ctx.mode})"
    )

    # --- Bootstrap ---
    # Universe selection goes through the injected selector unconditionally.
    # Live default = LLMUniverseSelector (functionally identical to the legacy
    # load_universe shim); backtest default = TopGainersUniverseSelector.
    # No branch on ``ctx.mode`` — DIP: orchestrator doesn't know modes.
    universe = await ctx.universe_selector.select(today)
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
    realized_pnl_total: Decimal = Decimal("0")

    starting_nav = (
        ctx.nav_override
        if ctx.nav_override is not None
        else await _get_nav(portfolio_id)
    )
    starting_nav_d = Decimal(str(starting_nav))
    circuit = CircuitState(
        starting_nav=starting_nav,
        lockin_target_pct=settings.laabh_quant_lockin_target_pct,
        kill_switch_dd_pct=settings.laabh_quant_kill_switch_dd_pct,
        cooloff_consecutive_losses=settings.laabh_quant_cooloff_consecutive_losses,
        cooloff_minutes=settings.laabh_quant_cooloff_minutes,
    )

    await _init_day_state(portfolio_id, today, starting_nav, universe, settings, ctx)

    # Crash recovery: rebuild in-memory state from any open / closed trades
    # already written for today (e.g. after a process restart mid-session).
    # Mutates `selector` to replay today's closed-trade rewards.
    recovered_open, recovered_pnl = await _load_open_positions(
        portfolio_id, today, selector
    )
    if recovered_open:
        open_positions.extend(recovered_open)
        logger.info(f"[QUANT] Recovered {len(recovered_open)} open trade(s) from DB")
    if recovered_pnl != 0:
        realized_pnl_total += recovered_pnl
        logger.info(
            f"[QUANT] Recovered ₹{float(recovered_pnl):.2f} realised P&L from earlier today"
        )

    # Compute session open once — 09:15 IST
    _session_open_ist_time = _IST.localize(
        datetime(today.year, today.month, today.day, 9, 15, 0)
    )

    replay_mode = as_of is not None
    # Time source: prefer ctx.clock (live default = LiveClock; backtest =
    # BacktestClockAdapter). The legacy ``as_of`` replay path is preserved
    # for backwards compatibility — when set, it overrides the clock.
    current_time = as_of if replay_mode else ctx.clock.now()
    poll_delta = timedelta(seconds=settings.laabh_quant_poll_interval_sec)
    hard_exit_time = settings.laabh_quant_hard_exit_time
    estimated_costs_per_lot = Decimal(str(settings.laabh_quant_estimated_costs_per_lot))
    max_loss_per_lot_pct = Decimal(str(settings.laabh_quant_max_loss_per_lot_pct))
    expected_gross_pnl_pct = Decimal(str(settings.laabh_quant_expected_gross_pnl_pct))
    # Keep last features_map for EOD close; initialise empty to avoid NameError
    features_map: dict[str, Any] = {}

    # --- Main loop ---
    while True:
        if not replay_mode:
            current_time = ctx.clock.now()

        now_ist = current_time.astimezone(_IST)

        if now_ist.time() >= hard_exit_time:
            break

        tick_start = current_time
        logger.debug(f"[QUANT] tick at {now_ist.strftime('%H:%M:%S')} IST")

        # Per-tick funnel-log scratch space. Populated as each gate fires so
        # the inner ``finally`` can hand a fully-classified set of rows to
        # the recorder regardless of which early-continue path we take.
        all_raw_signals: list[tuple[str, str, uuid.UUID, str, Any]] = []
        tick_disposition: dict[str, str] = {}
        strong_arm_set: set[str] = set()
        chosen_arm_for_log: str | None = None
        lots_for_log: int | None = None
        # Decision-Inspector trace scratch space. Allocated only when the
        # recorder will consume them (backtest mode); live mode keeps these
        # empty/None so the trace formatting cost is zero in production.
        trace_enabled: bool = ctx.mode == "backtest"
        primitive_traces: dict[str, dict] = {}
        bandit_trace_full: dict | None = None
        sizer_trace_full: dict | None = None

        try:
          try:
            # 1. Refresh features for each underlying
            features_map = {}
            for u in universe:
                bundle = await ctx.feature_getter(u["id"], current_time)
                if bundle:
                    features_map[u["symbol"]] = bundle
                    sym_hist = history[u["symbol"]]
                    sym_hist.append(bundle)
                    if len(sym_hist) > max_history_bars:
                        del sym_hist[0]

            # 2. Compute signals from each enabled primitive × each underlying.
            #    Capture EVERY non-None primitive output (incl. weak) for the
            #    funnel log; the strength gate produces the working ``signals``
            #    list the rest of the loop uses.
            signals: list[tuple[str, str, Any]] = []  # (arm_id, symbol, signal)
            for u in universe:
                symbol = u["symbol"]
                bundle = features_map.get(symbol)
                if bundle is None:
                    continue
                hist = history[symbol][:-1]  # exclude current bundle
                for prim in primitives:
                    arm_id = _make_arm_id(symbol, prim.name)
                    # Trace dict is allocated only in backtest mode. Each
                    # primitive populates its own keys (name/inputs/
                    # intermediates/formula). When live, ``ptrace`` stays
                    # None and primitives short-circuit the formatting.
                    ptrace: dict | None = {} if trace_enabled else None
                    sig = prim.compute_signal(bundle, hist, trace=ptrace)
                    if sig is None:
                        continue
                    all_raw_signals.append((arm_id, symbol, u["id"], prim.name, sig))
                    if ptrace:
                        primitive_traces[arm_id] = ptrace
                    if abs(sig.strength) >= settings.laabh_quant_min_signal_strength:
                        signals.append((arm_id, symbol, sig))
                        strong_arm_set.add(arm_id)
                    else:
                        tick_disposition[arm_id] = "weak_signal"

            # 3. Manage existing positions — close before NAV refresh so MTM
            #    only reflects still-open positions.
            for pos in list(open_positions):
                symbol = arm_meta.get(pos.arm_id, (pos.arm_id, ""))[0]
                bundle = features_map.get(symbol)
                if bundle is None:
                    continue
                current_premium = _get_premium_from_bundle(pos, bundle)
                arm_signals = [(aid, sig) for aid, _, sig in signals if aid == pos.arm_id]
                close, reason = should_close(
                    pos, current_premium, bundle.realized_vol_3min, current_time, arm_signals,
                    hard_exit_time=hard_exit_time,
                )
                if close:
                    pnl = await _close_position(
                        pos, current_premium, reason, portfolio_id, current_time, ctx
                    )
                    open_positions.remove(pos)
                    realized_pnl_total += pnl
                    # Reward = per-trade return ratio (lots-independent so the
                    # bandit posterior tracks per-lot edge, not capital usage).
                    reward = (
                        float(current_premium - pos.entry_premium_net) / float(pos.entry_premium_net)
                        if pos.entry_premium_net else 0.0
                    )
                    selector.update(pos.arm_id, reward)
                    if pnl > 0:
                        circuit.record_win(pos.arm_id)
                    else:
                        circuit.record_loss(pos.arm_id, current_time)

            # 4. Live NAV — in-memory: starting + realised + MTM(open).
            mtm = Decimal("0")
            for pos in open_positions:
                symbol = arm_meta.get(pos.arm_id, (pos.arm_id, ""))[0]
                bundle = features_map.get(symbol)
                if bundle is None:
                    continue
                cur = _get_premium_from_bundle(pos, bundle)
                mtm += (cur - pos.entry_premium_net) * Decimal(pos.lots)
            live_nav_d = starting_nav_d + realized_pnl_total + mtm
            current_nav = float(live_nav_d)
            circuit.check_and_fire(current_nav, current_time)
            day_running_pnl_pct = (
                (current_nav - starting_nav) / starting_nav if starting_nav else 0.0
            )

            # 5. Day-level circuit breaker
            if circuit.kill_active:
                logger.info("[QUANT] Kill switch active — skipping new entries this tick")
                for _aid in strong_arm_set:
                    tick_disposition.setdefault(_aid, "kill_switch")
                await _sleep_tick(settings, tick_start, replay_mode, ctx)
                if replay_mode:
                    current_time += poll_delta
                continue

            # 6. Capacity gate
            if len(open_positions) >= settings.laabh_quant_max_concurrent_positions:
                for _aid in strong_arm_set:
                    tick_disposition.setdefault(_aid, "capacity_full")
                await _sleep_tick(settings, tick_start, replay_mode, ctx)
                if replay_mode:
                    current_time += poll_delta
                continue

            # 7. First-entry warmup gate
            minutes_since_open = (now_ist - _session_open_ist_time).total_seconds() / 60.0
            if minutes_since_open < settings.laabh_quant_first_entry_after_minutes:
                for _aid in strong_arm_set:
                    tick_disposition.setdefault(_aid, "warmup")
                await _sleep_tick(settings, tick_start, replay_mode, ctx)
                if replay_mode:
                    current_time += poll_delta
                continue

            # 8. Bandit selection — build context for LinTS
            signalling_arms = [arm_id for arm_id, _, _ in signals]
            active_arms = [
                a for a in signalling_arms
                if not circuit.arm_in_cooloff(a, current_time)
            ]
            # Mark every arm filtered by cooloff; the rest may still be picked
            for _aid in signalling_arms:
                if _aid not in active_arms:
                    tick_disposition.setdefault(_aid, "cooloff")
            if not active_arms:
                await _sleep_tick(settings, tick_start, replay_mode, ctx)
                if replay_mode:
                    current_time += poll_delta
                continue

            signal_strengths = {arm_id: sig.strength for arm_id, _, sig in signals}
            context = _build_tick_context(
                features_map=features_map,
                minutes_since_open=minutes_since_open,
                day_running_pnl_pct=day_running_pnl_pct,
            )
            # Allocate the bandit-trace dict only when the recorder will use
            # it. The selector populates it with the full per-arm tournament
            # so the Decision Inspector can render the bandit card.
            bandit_trace_full = {} if trace_enabled else None
            chosen_arm = selector.select(
                active_arms,
                context=context,
                signal_strengths=signal_strengths,
                trace=bandit_trace_full,
            )
            if chosen_arm is None:
                # Bandit declined to pick any arm this tick — every active arm
                # lost the draw.
                for _aid in active_arms:
                    tick_disposition.setdefault(_aid, "lost_bandit")
                await _sleep_tick(settings, tick_start, replay_mode, ctx)
                if replay_mode:
                    current_time += poll_delta
                continue

            # Bandit picked one — every other active arm lost the draw.
            for _aid in active_arms:
                if _aid != chosen_arm:
                    tick_disposition.setdefault(_aid, "lost_bandit")
            chosen_arm_for_log = chosen_arm

            # 9. Size and open position
            chosen_entry = next((sig for arm_id, _, sig in signals if arm_id == chosen_arm), None)
            if chosen_entry is None:
                # Defensive — chosen arm has no signal in the working list
                # (shouldn't happen given upstream filtering).
                tick_disposition.setdefault(chosen_arm, "lost_bandit")
                await _sleep_tick(settings, tick_start, replay_mode, ctx)
                if replay_mode:
                    current_time += poll_delta
                continue

            capital = live_nav_d
            max_loss_per_lot = capital * max_loss_per_lot_pct
            estimated_costs = estimated_costs_per_lot
            expected_gross = (
                capital * expected_gross_pnl_pct * Decimal(str(chosen_entry.strength))
            )

            sizer_trace_full = {} if trace_enabled else None
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
                trace=sizer_trace_full,
            )

            lots_for_log = lots
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
                    estimated_costs_per_lot=estimated_costs_per_lot,
                    ctx=ctx,
                )
                if pos:
                    open_positions.append(pos)
                tick_disposition.setdefault(chosen_arm, "opened")
            else:
                tick_disposition.setdefault(chosen_arm, "sized_zero")
          finally:
            # Always flush the per-tick funnel log — runs on natural fall-
            # through, on every early ``continue``, and on exceptions raised
            # during the body. Best-effort; helper swallows write failures.
            await _emit_signal_log(
                ctx=ctx,
                virtual_time=current_time,
                all_raw_signals=all_raw_signals,
                tick_disposition=tick_disposition,
                strong_arm_set=strong_arm_set,
                selector=selector,
                chosen_arm=chosen_arm_for_log,
                lots=lots_for_log,
                primitive_traces=primitive_traces,
                bandit_trace_full=bandit_trace_full,
                sizer_trace_full=sizer_trace_full,
            )

        except Exception as exc:
            logger.exception(
                f"[QUANT] Tick failed at {now_ist.strftime('%H:%M:%S')}: {exc!r}"
            )

        await _sleep_tick(settings, tick_start, replay_mode, ctx)
        if replay_mode:
            current_time += poll_delta

    # --- End of day ---
    # features_map holds last tick's data; acceptable for paper-trading EOD close
    # (worst case: position opened on final tick uses entry_premium_net as exit
    # price → P&L=0).
    logger.info(f"[QUANT] Hard exit time reached — closing {len(open_positions)} positions")
    for pos in list(open_positions):
        symbol = arm_meta.get(pos.arm_id, (pos.arm_id, ""))[0]
        bundle = features_map.get(symbol)
        premium = _get_premium_from_bundle(pos, bundle) if bundle else pos.entry_premium_net
        try:
            pnl = await _close_position(pos, premium, "time_stop", portfolio_id, current_time, ctx)
            realized_pnl_total += pnl
        except Exception as exc:
            logger.error(
                f"[QUANT] EOD close failed for {pos.arm_id}: {exc!r} "
                f"— trade row left as 'open'; will be picked up by tomorrow's recovery"
            )

    final_nav = float(starting_nav_d + realized_pnl_total)
    await _finalize_day_state(portfolio_id, today, final_nav, starting_nav, circuit, ctx)
    await persistence.save_eod(
        portfolio_id, today, selector, all_arms, underlying_map
    )
    if ctx.mode != "backtest":
        # Backtest reporting is handled centrally by BacktestRunner;
        # per-day EOD reports would noise up the runs/ directory.
        await reports.generate_eod(portfolio_id, today)
    logger.info(f"[QUANT] Day complete. Final NAV={final_nav:.2f}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _total_exposure(positions: list[OpenPosition]) -> Decimal:
    """Total open exposure in ₹ (entry premium × lots, summed)."""
    return sum(
        (p.entry_premium_net * Decimal(p.lots) for p in positions),
        Decimal("0"),
    )


async def _load_open_positions(
    portfolio_id: uuid.UUID,
    today: date,
    selector,
) -> tuple[list[OpenPosition], Decimal]:
    """Rebuild open positions and today's realised P&L from the DB, and
    replay today's already-closed trades against *selector*.

    Used at startup to recover from a mid-session process restart. Returns
    (open_positions, realised_pnl_total_today). peak_premium and
    initial_risk_r are reset to conservative defaults; the trailing stop will
    re-arm naturally as new ticks arrive.
    """
    from src.db import session_scope
    from sqlalchemy import select
    from src.models.quant_trade import QuantTrade
    from src.quant.reports import _day_start

    open_pos: list[OpenPosition] = []
    realised = Decimal("0")
    day_start = _day_start(today)

    async with session_scope() as session:
        q_open = (
            select(QuantTrade)
            .where(QuantTrade.portfolio_id == portfolio_id)
            .where(QuantTrade.entry_at >= day_start)
            .where(QuantTrade.status == "open")
        )
        for trade in (await session.execute(q_open)).scalars():
            direction = _normalize_direction(trade.direction)
            if direction is None:
                logger.warning(
                    f"[QUANT] Skipping recovery of trade {trade.id}: "
                    f"unrecognised direction {trade.direction!r}"
                )
                continue
            pos = OpenPosition(
                arm_id=trade.arm_id,
                underlying_id=str(trade.underlying_id),
                direction=direction,  # type: ignore[arg-type]
                entry_premium_net=trade.entry_premium_net,
                entry_at=trade.entry_at,
                lots=trade.lots,
                trade_id=trade.id,
            )
            pos.initial_risk_r = trade.entry_premium_net * Decimal("0.2")
            open_pos.append(pos)

        q_closed = (
            select(QuantTrade)
            .where(QuantTrade.portfolio_id == portfolio_id)
            .where(QuantTrade.entry_at >= day_start)
            .where(QuantTrade.status == "closed")
        )
        closed_trades = list((await session.execute(q_closed)).scalars())
        for trade in closed_trades:
            if trade.realized_pnl is not None:
                realised += trade.realized_pnl

    if closed_trades:
        _replay_bandit_updates(selector, closed_trades)
        logger.info(
            f"[QUANT] Replayed {len(closed_trades)} closed-today trade(s) "
            f"into bandit selector"
        )

    return open_pos, realised


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
    estimated_costs_per_lot: Decimal,
    ctx: OrchestratorContext,
) -> OpenPosition | None:
    """Record a new trade via the recorder and return an in-memory OpenPosition."""
    if underlying_id is None:
        logger.error(f"[QUANT] No underlying_id for {arm_id} — skipping open")
        return None
    if signal.direction not in ("bullish", "bearish"):
        logger.error(f"[QUANT] Unexpected signal direction {signal.direction!r} for {arm_id}")
        return None

    entry_premium = _get_premium_from_bundle_stub(entry_bundle)
    bandit_seed = _seed_for_arm(portfolio_id, arm_id, now.date())
    estimated_costs = estimated_costs_per_lot * Decimal(lots)

    payload = OpenTradePayload(
        portfolio_id=portfolio_id,
        underlying_id=underlying_id,
        primitive_name=primitive_name,
        arm_id=arm_id,
        direction=signal.direction,
        entry_at=now,
        entry_premium_net=entry_premium,
        estimated_costs=estimated_costs,
        signal_strength_at_entry=signal.strength,
        posterior_mean_at_entry=posterior_mean,
        sampled_mean_at_entry=posterior_mean,
        bandit_seed=bandit_seed,
        kelly_fraction=kelly_fraction,
        lots=lots,
        legs={},
    )
    trade_id = await ctx.recorder.open_trade(payload)
    if trade_id is None:
        return None

    pos = OpenPosition(
        arm_id=arm_id,
        underlying_id=str(underlying_id),
        direction=signal.direction,
        entry_premium_net=entry_premium,
        entry_at=now,
        lots=lots,
        trade_id=trade_id,
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
    ctx: OrchestratorContext,
) -> Decimal:
    """Delegate the close to the recorder and return realised P&L."""
    pnl = (exit_premium - pos.entry_premium_net) * Decimal(pos.lots)
    try:
        await ctx.recorder.close_trade(
            CloseTradePayload(
                trade_id=pos.trade_id,
                arm_id=pos.arm_id,
                portfolio_id=portfolio_id,
                exit_at=now,
                exit_premium_net=exit_premium,
                realized_pnl=pnl,
                exit_reason=reason,
            )
        )
    except Exception as exc:
        # Surface the failure so the caller's outer try/except keeps the
        # position in memory; otherwise we'd lose track of it (orphan row in
        # DB, ghost in memory) and double-count P&L on next restart.
        logger.error(f"[QUANT] Failed to close position {pos.arm_id}: {exc!r}")
        raise

    logger.info(f"[QUANT] Closed {pos.arm_id} × {pos.lots} → P&L={pnl:.2f} ({reason})")
    return pnl


async def _init_day_state(
    portfolio_id: uuid.UUID,
    today: date,
    starting_nav: float,
    universe: list[dict],
    settings,
    ctx: OrchestratorContext,
) -> None:
    """Delegate per-day setup to the recorder (live → quant_day_state, backtest → no-op)."""
    payload = DayInitPayload(
        portfolio_id=portfolio_id,
        trading_date=today,
        starting_nav=starting_nav,
        universe=universe,
        config_snapshot={
            "primitives": settings.quant_primitives_list,
            "bandit_algo": settings.laabh_quant_bandit_algo,
            "forget_factor": settings.laabh_quant_bandit_forget_factor,
            "lockin_target_pct": settings.laabh_quant_lockin_target_pct,
            "kill_switch_pct": settings.laabh_quant_kill_switch_dd_pct,
        },
        bandit_seed=settings.laabh_quant_bandit_seed or 0,
    )
    await ctx.recorder.init_day(payload)


async def _finalize_day_state(
    portfolio_id: uuid.UUID,
    today: date,
    final_nav: float,
    starting_nav: float,
    circuit: CircuitState,
    ctx: OrchestratorContext,
) -> None:
    """Delegate per-day teardown — recorder updates the right ledger.

    Trade-count is computed from the same ledger the recorder writes to.
    For live: ``quant_trades`` (existing behavior). For backtest: the
    recorder counts via ``backtest_trades`` for its own run_id.
    """
    from src.db import session_scope
    from sqlalchemy import select, func
    from src.models.quant_trade import QuantTrade
    from src.quant.reports import _day_start

    # Live recorder needs an accurate trade count — query the live ledger.
    # Backtest recorder ignores the count we pass and queries its own ledger.
    trade_count = 0
    if ctx.mode != "backtest":
        async with session_scope() as session:
            q = select(func.count()).where(
                QuantTrade.portfolio_id == portfolio_id,
                QuantTrade.entry_at >= _day_start(today),
            )
            trade_count = (await session.execute(q)).scalar() or 0

    payload = DayFinalizePayload(
        portfolio_id=portfolio_id,
        trading_date=today,
        final_nav=final_nav,
        starting_nav=starting_nav,
        lockin_fired_at=circuit.lockin_fired_at,
        kill_switch_fired_at=circuit.kill_fired_at,
        trade_count=trade_count,
    )
    await ctx.recorder.finalize_day(payload)


async def _sleep_tick(
    settings,
    tick_start: datetime,
    replay_mode: bool,
    ctx: OrchestratorContext,
) -> None:
    """Delegate the tick wait to the injected clock.

    Live clock blocks on ``asyncio.sleep``; backtest adapter advances virtual
    time. In replay mode the orchestrator advances ``current_time`` itself
    (legacy path, used by tests and by ``BacktestRunner``); we still need
    to keep the clock's virtual state synchronised so consumers like
    ``LookaheadGuard`` don't see the clock frozen at session open. The
    ``advance(seconds)`` call is a no-op on the LiveClock and a virtual
    advance on the BacktestClockAdapter — preserving both behaviors.
    """
    if replay_mode:
        ctx.clock.advance(settings.laabh_quant_poll_interval_sec)
        return
    await ctx.clock.sleep_until_next_tick(
        tick_start=tick_start,
        poll_seconds=settings.laabh_quant_poll_interval_sec,
    )
