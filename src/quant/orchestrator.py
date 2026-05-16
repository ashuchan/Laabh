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
    selector.update(arm_id, (exit-entry)/entry, context=trade.entry_context)
    — the same per-lot return ratio + entry context the live tick path uses,
    so n_obs and posterior_mean stay consistent. Skips trades with missing
    exit_premium or zero entry to keep the function total.

    Trades opened before the ``entry_context`` column existed get a None
    context, which the LinTS update treats as a zero-vector — a legacy
    no-op rather than a crash. The reward still increments n_obs but
    won't shift the posterior; acceptable for a one-time backfill.
    """
    for trade in sorted(closed_trades, key=lambda t: t.entry_at):
        entry = trade.entry_premium_net
        exit_ = trade.exit_premium_net
        if exit_ is None or not entry:
            continue
        reward = float(exit_ - entry) / float(entry)
        ctx_list = getattr(trade, "entry_context", None)
        ctx_array = np.array(ctx_list, dtype=float) if ctx_list else None
        selector.update(trade.arm_id, reward, context=ctx_array)


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
    The shared base used by all arms; per-arm LLM augmentation happens in
    :func:`_build_per_arm_contexts` when LAABH_LLM_MODE='feature'.
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


async def _build_per_arm_contexts(
    *,
    base_context: np.ndarray,
    active_arms: list[str],
    arm_meta: dict[str, tuple[str, str]],
    underlying_map: dict[str, "uuid.UUID"],
    run_date: "date",
) -> tuple[dict[str, np.ndarray], dict[str, int | None]]:
    """Return (per_arm_contexts, per_arm_log_ids) under LAABH_LLM_MODE='feature'.

    For each active arm, look up the latest calibrated LLM-feature row for
    its underlying on ``run_date`` and append four LLM dims to the base
    5-dim context. Arms whose underlying lacks an LLM row get a neutral
    zero-tail (no LLM information ⇒ no LLM influence on that arm's score).

    The second return value carries the source ``llm_decision_log.id`` per
    arm so the orchestrator can write the bandit propensity back to that
    specific row at decision time (IPS reweighting input).
    """
    from src.fno.llm_feature_lookup import get_latest_features

    per_arm_ctx: dict[str, np.ndarray] = {}
    per_arm_log: dict[str, int | None] = {}

    for arm_id in active_arms:
        symbol, _primitive = arm_meta.get(arm_id, (arm_id, ""))
        underlying_id = underlying_map.get(symbol)
        if underlying_id is None:
            per_arm_ctx[arm_id] = np.concatenate([base_context, np.zeros(4)])
            per_arm_log[arm_id] = None
            continue
        features = await get_latest_features(underlying_id, run_date)
        # Direct concat — the base 5-dim vector is already normalised by
        # build_context, and LLM features are already bounded by the
        # calibration step. No round-trip through build_context_with_llm
        # (review fix P3 #11).
        llm_tail = np.array([
            features.calibrated_conviction,
            features.thesis_durability,
            features.catalyst_specificity,
            features.risk_flag,
        ], dtype=float)
        per_arm_ctx[arm_id] = np.concatenate([base_context, llm_tail])
        per_arm_log[arm_id] = features.log_id if features.is_present else None

    return per_arm_ctx, per_arm_log


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

    # Schema guard: validate the live feature_store SQL probes before the loop
    # begins. A column-name regression here used to silently kill every tick
    # via the catch-all except; now it aborts startup with a clear error.
    if ctx.mode == "live":
        await feature_store.ensure_schema()

    # --- Bootstrap ---
    # Universe selection goes through the injected selector unconditionally.
    # Live default = LLMUniverseSelector (functionally identical to the legacy
    # load_universe shim); backtest default = TopGainersUniverseSelector.
    # No branch on ``ctx.mode`` — DIP: orchestrator doesn't know modes.
    universe = await ctx.universe_selector.select(today)
    if not universe:
        logger.warning("[QUANT] Empty universe — aborting orchestrator loop")
        return

    # Effective primitives list — backtest contexts can supply an override
    # (Phase 4 fix) to drop primitives that are guaranteed silent under
    # backtest data (e.g. OFI without L1 quotes). Live and other callers
    # leave the override None and inherit ``settings.quant_primitives_list``.
    effective_primitives_list = (
        ctx.primitives_override
        if ctx.primitives_override is not None
        else settings.quant_primitives_list
    )

    underlying_map: dict[str, uuid.UUID] = {u["symbol"]: u["id"] for u in universe}
    all_arms = [
        _make_arm_id(u["symbol"], p)
        for u in universe
        for p in effective_primitives_list
    ]

    # Pre-compute arm → (symbol, primitive_name) to avoid per-tick string splitting
    arm_meta: dict[str, tuple[str, str]] = {
        _make_arm_id(u["symbol"], p): (u["symbol"], p)
        for u in universe
        for p in effective_primitives_list
    }

    selector = await persistence.load_morning(
        portfolio_id, today, all_arms, underlying_map,
        as_of=as_of, dryrun_run_id=dryrun_run_id,
    )

    primitives = _load_primitives(effective_primitives_list)
    # Lookup table for primitive-aware exit dispatch (Phase 3 take-profit).
    primitives_by_name = {p.name: p for p in primitives}
    # History cap = the largest primitive's warmup (in BARS — was previously
    # divided by 3 because the field was misnamed ``warmup_minutes``; that
    # bug permanently blocked momentum (needs 11 bars) and vol_breakout
    # (needs 20 bars). +2 buffer so the per-tick ``hist[:-1]`` slice still
    # leaves enough bars for the largest warmup.
    max_history_bars = max((p.warmup_bars for p in primitives), default=10) + 2
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

    await _init_day_state(
        portfolio_id, today, starting_nav, universe, settings, ctx,
        primitives_list=effective_primitives_list,
    )

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

    # --- Intraday scanner setup (live quant mode only) ---
    # The scanner is skipped entirely in backtest/replay mode — intraday data
    # for future dates is not available, and universe expansion during replay
    # would introduce look-ahead bias.
    scanner = None
    scan_interval = timedelta(minutes=settings.laabh_quant_intraday_scanner_interval_min)
    # Seed last_scan_time so the first scan fires ~2 min after startup (≈09:20 IST)
    # rather than immediately at 09:18. By 09:20 the opening 3-min bar is settled
    # and momentum readings are meaningful. Without this seed the scan would run
    # on the very first tick with an empty eviction pool anyway, but the DB
    # round-trip is wasted.
    last_scan_time: datetime | None = None
    if (
        ctx.mode == "live"
        and not replay_mode
        and settings.laabh_quant_intraday_scanner_enabled
    ):
        from src.quant.live_gainers_scanner import LiveGainersScanner
        scanner = LiveGainersScanner()
        # Pre-set so first real scan fires scan_interval - 2 min after startup
        last_scan_time = current_time - scan_interval + timedelta(minutes=2)
        logger.info("[QUANT] Intraday universe scanner enabled (first scan ~2 min after start)")

    # --- Main loop ---
    while True:
        if not replay_mode:
            current_time = ctx.clock.now()

        now_ist = current_time.astimezone(_IST)

        if now_ist.time() >= hard_exit_time:
            break

        tick_start = current_time
        logger.debug(f"[QUANT] tick at {now_ist.strftime('%H:%M:%S')} IST")

        # --- Intraday scanner: replace weak arms with live movers ---
        # Runs at most once per scan_interval. Only fires in live mode (scanner
        # is None in backtest/replay). Arm mutations happen here, before feature
        # reads, so the tick immediately benefits from any new instrument.
        if scanner is not None and (
            last_scan_time is None
            or (current_time - last_scan_time) >= scan_interval
        ):
            try:
                # Resolve symbols from arm_meta to handle underscores in names
                # (e.g. BAJAJ_AUTO). arm_id.split("_")[0] would be wrong here.
                open_position_symbols = {
                    arm_meta.get(pos.arm_id, (pos.arm_id, ""))[0]
                    for pos in open_positions
                }
                pairs = await scanner.compute_replacements(
                    universe,
                    selector,
                    open_position_symbols,
                    trading_date=today,
                    primitives_list=effective_primitives_list,
                )
                for pair in pairs:
                    evict_sym = pair.evict_symbol
                    admit = pair.admit_instrument
                    # Evict all primitive arms for evicted symbol
                    for p in effective_primitives_list:
                        selector.evict_arm(_make_arm_id(evict_sym, p))
                    # Remove evicted symbol from universe and its history
                    universe = [u for u in universe if u["symbol"] != evict_sym]
                    history.pop(evict_sym, None)
                    # Admit new symbol
                    universe.append(admit)
                    history[admit["symbol"]] = []
                    underlying_map[admit["symbol"]] = admit["id"]
                    for p in effective_primitives_list:
                        new_arm = _make_arm_id(admit["symbol"], p)
                        warm = selector.admit_arm(new_arm)
                        arm_meta[new_arm] = (admit["symbol"], p)
                        all_arms.append(new_arm)
                        logger.info(
                            f"[SCANNER] Admitted {new_arm} "
                            f"({'warm' if warm else 'cold'} prior)"
                        )
                    # Clean up evicted arm meta + arm list
                    for p in effective_primitives_list:
                        old_arm = _make_arm_id(evict_sym, p)
                        arm_meta.pop(old_arm, None)
                        if old_arm in all_arms:
                            all_arms.remove(old_arm)
                    underlying_map.pop(evict_sym, None)
                # Only advance last_scan_time on a successful cycle so that a
                # transient DB failure retries on the next tick, not after a
                # full scan_interval.
                last_scan_time = current_time
            except Exception as _scan_exc:
                logger.warning(f"[SCANNER] scan cycle failed: {_scan_exc!r}")

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
            # 1. Refresh features for each underlying. Errors are isolated per
            # symbol so one bad fetch (transient DB hiccup, missing chain row)
            # doesn't tank every primitive on the tick.
            features_map = {}
            for u in universe:
                try:
                    bundle = await ctx.feature_getter(u["id"], current_time)
                except Exception as fetch_exc:
                    logger.warning(
                        f"[QUANT] feature fetch failed for {u['symbol']}: "
                        f"{fetch_exc!r}"
                    )
                    continue
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
                symbol, primitive_name = arm_meta.get(pos.arm_id, (pos.arm_id, ""))
                bundle = features_map.get(symbol)
                if bundle is None:
                    continue
                current_premium = _get_premium_from_bundle(pos, bundle)
                arm_signals = [(aid, sig) for aid, _, sig in signals if aid == pos.arm_id]
                # Phase 3 — primitive-aware take-profit hook. The primitive
                # owns its entry hypothesis; if its definition of "we got
                # what we came for" is met, close now with reason
                # ``take_profit`` regardless of the generic stop policy.
                # Default ``BasePrimitive.should_take_profit`` returns False,
                # so primitives without an override fall straight through
                # to ``should_close``.
                prim = primitives_by_name.get(primitive_name)
                if prim is not None and prim.should_take_profit(pos, bundle):
                    close, reason = True, "take_profit"
                else:
                    close, reason = should_close(
                        pos, current_premium, bundle.realized_vol_3min,
                        current_time, arm_signals,
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
                    # Replay the entry-time context so LinTS updates against
                    # the same vector it sampled (review fix P0 #1).
                    selector.update(pos.arm_id, reward, context=pos.entry_context)
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
            # Phase 3.4 (review fix P2 #8): when LAABH_LLM_MODE='feature',
            # reserve 1 capacity slot for high-posterior-variance arms so
            # exploration trades don't get crowded out by high-conviction
            # trades. At max-1 occupancy we narrow the active arm set to
            # the top-quartile-variance arms; at max we still gate.
            max_concurrent = settings.laabh_quant_max_concurrent_positions
            llm_feature_mode = settings.laabh_llm_mode == "feature"
            reserve_slot_active = (
                llm_feature_mode
                and len(open_positions) == max_concurrent - 1
                and max_concurrent >= 2
            )
            if len(open_positions) >= max_concurrent:
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
            # Phase 3 cutover: LAABH_LLM_MODE='feature' augments the context
            # with calibrated LLM dims, one per arm. Build BEFORE the
            # reserved-slot gate so the exploration-variance ranking uses
            # the same vectors the selector will sample with.
            per_arm_contexts: dict[str, "np.ndarray"] | None = None
            per_arm_log_ids: dict[str, int | None] = {}
            if settings.laabh_llm_mode == "feature":
                per_arm_contexts, per_arm_log_ids = await _build_per_arm_contexts(
                    base_context=context,
                    active_arms=active_arms,
                    arm_meta=arm_meta,
                    underlying_map=underlying_map,
                    run_date=today,
                )

            # Phase 3.4 exploration-slot reservation (review fix P2 #8):
            # when the reserve is active, narrow active_arms to the top
            # quartile by *contextual* posterior variance x^T A_inv x
            # (review fix P3 #6 — uses the right quantity instead of the
            # mean-of-diagonal proxy).
            if reserve_slot_active and len(active_arms) >= 4:
                if per_arm_contexts is not None:
                    variances = [
                        (a, selector.posterior_var_for_context(a, per_arm_contexts[a]))
                        for a in active_arms
                    ]
                else:
                    variances = [(a, selector.posterior_var(a)) for a in active_arms]
                variances.sort(key=lambda t: t[1], reverse=True)
                cutoff = max(1, len(variances) // 4)
                high_var_set = {a for a, _ in variances[:cutoff]}
                for a in active_arms:
                    if a not in high_var_set:
                        tick_disposition.setdefault(a, "reserved_slot_skipped")
                active_arms = [a for a in active_arms if a in high_var_set]
                # Narrow per_arm_contexts / log_ids to the surviving arms
                # so the bandit and the propensity write stay consistent.
                if per_arm_contexts is not None:
                    per_arm_contexts = {a: per_arm_contexts[a] for a in active_arms}
                    per_arm_log_ids = {a: per_arm_log_ids.get(a) for a in active_arms}
                if not active_arms:
                    await _sleep_tick(settings, tick_start, replay_mode, ctx)
                    if replay_mode:
                        current_time += poll_delta
                    continue

            # Allocate the bandit-trace dict only when the recorder will use
            # it. The selector populates it with the full per-arm tournament
            # so the Decision Inspector can render the bandit card.
            bandit_trace_full = {} if trace_enabled else None
            chosen_arm = selector.select(
                active_arms,
                context=context,
                signal_strengths=signal_strengths,
                trace=bandit_trace_full,
                contexts=per_arm_contexts,
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
                # Pick the context the selector actually used for this arm
                # so the post-close update sees the same vector (P0 #1).
                chosen_context = (
                    per_arm_contexts[chosen_arm]
                    if per_arm_contexts is not None and chosen_arm in per_arm_contexts
                    else context
                )
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
                    entry_context=chosen_context,
                )
                if pos:
                    open_positions.append(pos)
                    # Phase 3: stash bandit propensity on the linked
                    # llm_decision_log row so IPS reweighting can use it.
                    # Only fires when feature mode actually attached an
                    # LLM-log id to this arm.
                    if (
                        settings.laabh_llm_mode == "feature"
                        and per_arm_log_ids.get(chosen_arm) is not None
                        and bandit_trace_full is not None
                    ):
                        try:
                            from src.fno.llm_feature_lookup import write_bandit_propensity
                            arms_block = bandit_trace_full.get("arms", {})
                            chosen_block = arms_block.get(chosen_arm, {})
                            others = [
                                a["posterior_mean"] for k, a in arms_block.items()
                                if k != chosen_arm and "posterior_mean" in a
                            ]
                            post_mean = float(chosen_block.get("posterior_mean", 0.0))
                            post_var = float(chosen_block.get("posterior_var", 1e-6))
                            tau = max(post_var ** 0.5, 1e-3)
                            # Softmax-equivalent propensity from logged
                            # posteriors (plan §2.2).
                            import math as _math
                            exps = [_math.exp(post_mean / tau)] + [
                                _math.exp(om / tau) for om in others
                            ]
                            denom = sum(exps) or 1.0
                            propensity = exps[0] / denom
                            await write_bandit_propensity(
                                per_arm_log_ids[chosen_arm],
                                posterior_mean=post_mean,
                                posterior_var=post_var,
                                propensity=propensity,
                            )
                        except Exception as exc:
                            logger.warning(f"[QUANT] propensity write failed: {exc!r}")
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
    entry_context: np.ndarray | None = None,
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

    entry_context_list = (
        entry_context.tolist() if entry_context is not None else None
    )
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
        entry_context=entry_context_list,
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
        entry_context=entry_context,
    )
    pos.initial_risk_r = entry_premium * Decimal("0.2")
    logger.info(f"[QUANT] Opened {arm_id} × {lots} lots @ {entry_premium}")
    await _notify_trade_open(
        arm_id=arm_id,
        direction=signal.direction,
        lots=lots,
        entry_premium=entry_premium,
        signal_strength=signal.strength,
        kelly_fraction=kelly_fraction,
        now=now,
        portfolio_id=portfolio_id,
    )
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
    await _notify_trade_close(
        arm_id=pos.arm_id,
        direction=pos.direction,
        lots=pos.lots,
        entry_premium=pos.entry_premium_net,
        exit_premium=exit_premium,
        pnl=pnl,
        reason=reason,
        entry_at=pos.entry_at,
        exit_at=now,
        portfolio_id=portfolio_id,
    )
    return pnl


_IST_TZ = pytz.timezone("Asia/Kolkata")


async def _portfolio_snapshot_text(portfolio_id: uuid.UUID, now: datetime) -> str:
    """Return a Telegram-friendly portfolio snapshot for today.

    Single cheap SELECT against quant_trades — caller-safe; any failure
    returns an empty string so the parent notification still goes out.
    """
    try:
        from sqlalchemy import text as _text
        from src.db import session_scope
        today_ist = now.astimezone(_IST_TZ).date()
        async with session_scope() as session:
            r = await session.execute(_text("""
                SELECT arm_id, direction, lots, status, entry_premium_net,
                       entry_at, realized_pnl, exit_reason
                FROM quant_trades
                WHERE portfolio_id = :pid
                  AND DATE(entry_at AT TIME ZONE 'Asia/Kolkata') = :d
                ORDER BY entry_at ASC
            """), {"pid": portfolio_id, "d": today_ist})
            rows = r.fetchall()
    except Exception as exc:
        logger.warning(f"[QUANT] portfolio snapshot query failed: {exc!r}")
        return ""

    open_rows = [row for row in rows if row.status == "open"]
    closed_rows = [row for row in rows if row.status == "closed"]
    realized = sum(float(row.realized_pnl or 0) for row in closed_rows)
    wins = sum(1 for row in closed_rows if (row.realized_pnl or 0) > 0)
    losses = sum(1 for row in closed_rows if (row.realized_pnl or 0) < 0)

    lines = ["", "📋 Portfolio"]
    if open_rows:
        lines.append(f"   Open positions: {len(open_rows)}")
        # Cap to 15 visible to keep the message under Telegram's 4096 limit
        for row in open_rows[:15]:
            age_min = (
                max(0.0, (now - row.entry_at).total_seconds() / 60.0)
                if row.entry_at else 0.0
            )
            lines.append(
                f"     • {row.arm_id:<22s} {row.direction.upper():<7s} × {row.lots}"
                f"   entry ₹{float(row.entry_premium_net):.2f}   ({age_min:.0f}m ago)"
            )
        if len(open_rows) > 15:
            lines.append(f"     … +{len(open_rows) - 15} more")
    else:
        lines.append("   Open positions: 0")
    lines.append(
        f"   Closed: {len(closed_rows)}  ({wins}W / {losses}L)   "
        f"Realized P&L: ₹{realized:+,.2f}"
    )
    return "\n".join(lines)


async def _notify_trade_open(
    *,
    arm_id: str,
    direction: str,
    lots: int,
    entry_premium: Decimal,
    signal_strength: float,
    kelly_fraction: float,
    now: datetime,
    portfolio_id: uuid.UUID,
) -> None:
    """Telegram ping on quant entry. Failures never block the trading loop."""
    try:
        from src.services.notification_service import NotificationService
        when = now.astimezone(_IST_TZ).strftime("%H:%M:%S IST")
        msg = (
            f"🟢 [QUANT] OPEN {arm_id}  {direction.upper()} × {lots} lots @ ₹{entry_premium:.2f}\n"
            f"   sig={signal_strength:.2f}  k={kelly_fraction:.2f}  {when}"
        )
        msg += await _portfolio_snapshot_text(portfolio_id, now)
        await NotificationService().send_text(msg, parse_mode=None)
    except Exception as exc:
        logger.warning(f"[QUANT] trade-open notification failed: {exc!r}")


async def _notify_trade_close(
    *,
    arm_id: str,
    direction: str,
    lots: int,
    entry_premium: Decimal,
    exit_premium: Decimal,
    pnl: Decimal,
    reason: str,
    entry_at: datetime,
    exit_at: datetime,
    portfolio_id: uuid.UUID,
) -> None:
    """Telegram ping on quant exit. Failures never block the trading loop."""
    try:
        from src.services.notification_service import NotificationService
        held_min = max(0.0, (exit_at - entry_at).total_seconds() / 60.0)
        when = exit_at.astimezone(_IST_TZ).strftime("%H:%M:%S IST")
        emoji = "🟢" if pnl >= 0 else "🔴"
        msg = (
            f"{emoji} [QUANT] CLOSE {arm_id}  {direction.upper()} × {lots}  P&L ₹{float(pnl):+,.2f}  ({reason})\n"
            f"   entry ₹{entry_premium:.2f} → exit ₹{exit_premium:.2f}  held {held_min:.0f}m  {when}"
        )
        msg += await _portfolio_snapshot_text(portfolio_id, exit_at)
        await NotificationService().send_text(msg, parse_mode=None)
    except Exception as exc:
        logger.warning(f"[QUANT] trade-close notification failed: {exc!r}")


async def _init_day_state(
    portfolio_id: uuid.UUID,
    today: date,
    starting_nav: float,
    universe: list[dict],
    settings,
    ctx: OrchestratorContext,
    *,
    primitives_list: list[str],
) -> None:
    """Delegate per-day setup to the recorder (live → quant_day_state, backtest → no-op).

    ``primitives_list`` is the *effective* list (after any context override
    is applied), so the persisted ``config_snapshot`` reflects what actually
    ran rather than the raw settings list — important for backtest reports.
    """
    payload = DayInitPayload(
        portfolio_id=portfolio_id,
        trading_date=today,
        starting_nav=starting_nav,
        universe=universe,
        config_snapshot={
            "primitives": list(primitives_list),
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
