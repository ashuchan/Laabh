"""Async read-only API for the Decision Inspector (PR 2).

Six entry points consumed by the Streamlit page (PR 3+):

  * ``list_runs(portfolio_id, *, limit)`` — run-picker dropdown
  * ``load_session_skeleton(run_id)`` — scrubber + summary
  * ``load_underlying_timeline(run_id, symbol)`` — price + VIX overlay
  * ``load_tick_state(run_id, virtual_time, symbol)`` — Tick Inspector waterfall
  * ``load_arm_history(run_id, arm_id)`` — heatmap rail
  * ``load_tick_diff(run_id, t1, t2, symbol)`` — diff strip

Design rules (committed in PR 2 self-review):
  * Every function opens its own ``session_scope`` — the Streamlit page is
    sync; bridging happens at the page boundary, not inside readers.
  * No caching here. PR 6 adds caching where measurement shows it's needed;
    speculative caching would obscure correctness.
  * ``FeatureBundle`` is recomputed on demand via ``BacktestFeatureStore`` —
    the harness doesn't persist it, and the lookahead guard already proves
    the store is virtual-time-faithful.
  * Pure mappers (``_row_to_*``) are pulled out so per-row transformation
    is unit-testable without a DB.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import pytz
from loguru import logger
from sqlalchemy import and_, asc, desc, func, select

from src.db import session_scope
from src.models.backtest_run import BacktestRun
from src.models.backtest_signal_log import BacktestSignalLog
from src.models.backtest_trade import BacktestTrade
from src.models.fno_vix import VIXTick
from src.models.instrument import Instrument
from src.models.price_intraday import PriceIntraday
from src.quant.backtest.feature_store import BacktestFeatureStore
from src.quant.feature_store import FeatureBundle
from src.quant.inspector.types import (
    ArmHistory,
    ArmTickState,
    BanditTournamentView,
    FeatureDelta,
    PriceBar,
    PrimitiveSignalView,
    RunMetadata,
    SessionSkeleton,
    SizerOutcomeView,
    TickDiff,
    TickState,
    TickSummary,
    TradeRecord,
    UnderlyingTimeline,
    UniverseEntry,
    VIXBar,
)


_IST = pytz.timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Pure mappers (DB row → dataclass). Pulled out for unit-testability.
# ---------------------------------------------------------------------------

def _to_run_metadata(r: BacktestRun) -> RunMetadata:
    """Project a ``backtest_runs`` row to ``RunMetadata`` (lightweight)."""
    return RunMetadata(
        run_id=r.id,
        portfolio_id=r.portfolio_id,
        backtest_date=r.backtest_date,
        started_at=r.started_at,
        completed_at=r.completed_at,
        starting_nav=float(r.starting_nav),
        final_nav=float(r.final_nav) if r.final_nav is not None else None,
        pnl_pct=float(r.pnl_pct) if r.pnl_pct is not None else None,
        trade_count=r.trade_count,
        bandit_seed=int(r.bandit_seed),
    )


def _to_universe(raw: list[dict] | None) -> list[UniverseEntry]:
    """Convert ``backtest_runs.universe`` JSONB to typed entries.

    The JSONB shape was chosen by the runner (``{id, symbol, name}``);
    we coerce ids to UUID here so callers don't need to know the wire format.
    """
    out: list[UniverseEntry] = []
    for u in raw or []:
        try:
            inst_id = u["id"] if isinstance(u["id"], uuid.UUID) else uuid.UUID(str(u["id"]))
        except (KeyError, ValueError, TypeError):
            continue
        out.append(
            UniverseEntry(
                instrument_id=inst_id,
                symbol=str(u.get("symbol", "")),
                name=u.get("name"),
            )
        )
    return out


def _to_trade_record(t: BacktestTrade) -> TradeRecord:
    return TradeRecord(
        trade_id=t.id,
        arm_id=t.arm_id,
        primitive_name=t.primitive_name,
        underlying_id=t.underlying_id,
        direction=t.direction,
        entry_at=t.entry_at,
        exit_at=t.exit_at,
        entry_premium_net=float(t.entry_premium_net),
        exit_premium_net=(
            float(t.exit_premium_net) if t.exit_premium_net is not None else None
        ),
        realized_pnl=(
            float(t.realized_pnl) if t.realized_pnl is not None else None
        ),
        lots=int(t.lots),
        exit_reason=t.exit_reason,
    )


def _to_primitive_signal(row: BacktestSignalLog) -> PrimitiveSignalView:
    return PrimitiveSignalView(
        arm_id=row.arm_id,
        primitive_name=row.primitive_name,
        direction=row.direction,
        strength=float(row.strength),
        rejection_reason=row.rejection_reason,
        posterior_mean=(
            float(row.posterior_mean) if row.posterior_mean is not None else None
        ),
        bandit_selected=bool(row.bandit_selected),
        lots_sized=row.lots_sized,
        primitive_trace=row.primitive_trace,
    )


def _aggregate_tick_summary(
    virtual_time: datetime, rows: list[BacktestSignalLog]
) -> TickSummary:
    """Fold a tick's signal-log rows into a single ``TickSummary``.

    The funnel taxonomy is closed (see migration 2026-05-10): every row's
    ``rejection_reason`` falls into one of the documented buckets. Counts
    here mirror that taxonomy 1-to-1.
    """
    n_total = len(rows)
    n_strong = sum(1 for r in rows if r.rejection_reason != "weak_signal")

    by_reason: dict[str, int] = defaultdict(int)
    for r in rows:
        by_reason[r.rejection_reason] += 1

    return TickSummary(
        virtual_time=virtual_time,
        n_signals_total=n_total,
        n_signals_strong=n_strong,
        n_opened=by_reason.get("opened", 0),
        n_lost_bandit=by_reason.get("lost_bandit", 0),
        n_sized_zero=by_reason.get("sized_zero", 0),
        n_cooloff=by_reason.get("cooloff", 0),
        n_kill_switch=by_reason.get("kill_switch", 0),
        n_capacity_full=by_reason.get("capacity_full", 0),
        n_warmup=by_reason.get("warmup", 0),
    )


def _reconstruct_tournament(
    rows: list[BacktestSignalLog],
) -> BanditTournamentView | None:
    """Build a ``BanditTournamentView`` by aggregating per-row slices.

    Each row's ``bandit_trace`` (when populated) carries a ``this_arm``
    payload + the shared context vector. We aggregate the per-arm payloads
    keyed by ``arm_id`` and pull the shared context off the first row that
    has it.

    Returns None when no row has a bandit_trace (i.e., this tick never
    reached the bandit — warmup / kill_switch / capacity_full / all-cooloff).
    """
    competing = [r for r in rows if r.bandit_trace]
    if not competing:
        return None

    # Shared fields come off any competing row — they're identical per tick
    # by construction (the orchestrator slices one full trace).
    head = competing[0].bandit_trace or {}

    arms_payload: dict[str, dict] = {}
    selected: str | None = None
    for r in competing:
        bt = r.bandit_trace or {}
        this_arm = bt.get("this_arm")
        if this_arm is not None:
            arms_payload[r.arm_id] = this_arm
        if r.bandit_selected:
            selected = r.arm_id

    return BanditTournamentView(
        algo=head.get("algo", "unknown"),
        context_vector=head.get("context_vector"),
        context_dims=head.get("context_dims"),
        arms=arms_payload,
        selected_arm_id=selected,
        n_competitors=int(head.get("n_competitors") or len(arms_payload)),
    )


def _extract_sizer_outcome(
    rows: list[BacktestSignalLog],
) -> tuple[SizerOutcomeView | None, str | None]:
    """Return (sizer view, chosen_arm_id) — both None when no entry occurred."""
    for r in rows:
        if r.sizer_trace and r.bandit_selected:
            sz = r.sizer_trace
            return (
                SizerOutcomeView(
                    final_lots=int(sz.get("final_lots", 0)),
                    blocking_step=sz.get("blocking_step"),
                    inputs=sz.get("inputs", {}),
                    constants=sz.get("constants", {}),
                    cascade=sz.get("cascade", []),
                ),
                r.arm_id,
            )
    return None, None


# ---------------------------------------------------------------------------
# 1. list_runs — for the run-picker dropdown
# ---------------------------------------------------------------------------

async def list_runs(
    portfolio_id: uuid.UUID,
    *,
    limit: int = 50,
) -> list[RunMetadata]:
    """Return recent backtest runs for a portfolio, newest first.

    Lightweight: no per-tick or trade aggregation. The dropdown should stay
    snappy even with hundreds of runs.
    """
    async with session_scope() as session:
        q = (
            select(BacktestRun)
            .where(BacktestRun.portfolio_id == portfolio_id)
            .order_by(desc(BacktestRun.started_at))
            .limit(limit)
        )
        rows = (await session.execute(q)).scalars().all()
    return [_to_run_metadata(r) for r in rows]


# ---------------------------------------------------------------------------
# 2. load_session_skeleton — scrubber + summary
# ---------------------------------------------------------------------------

async def load_session_skeleton(run_id: uuid.UUID) -> SessionSkeleton | None:
    """Load run metadata + per-tick summary + all trades for one run.

    Returns None when the run_id doesn't exist. Three queries: the run row,
    the signal-log rows (aggregated in Python by virtual_time), and the
    trade rows. A fourth query runs only when the persisted ``universe``
    JSONB is empty — see ``_derive_universe_from_signal_log``.
    """
    async with session_scope() as session:
        run = await session.get(BacktestRun, run_id)
        if run is None:
            return None

        # Pull only the columns we need for the summary aggregation —
        # avoids deserialising the JSONB trace blobs into Python objects
        # (which is what dominates per-row cost on this table).
        sig_q = (
            select(BacktestSignalLog)
            .where(BacktestSignalLog.backtest_run_id == run_id)
            .order_by(asc(BacktestSignalLog.virtual_time))
        )
        sig_rows = (await session.execute(sig_q)).scalars().all()

        trade_q = (
            select(BacktestTrade)
            .where(BacktestTrade.backtest_run_id == run_id)
            .order_by(asc(BacktestTrade.entry_at))
        )
        trade_rows = (await session.execute(trade_q)).scalars().all()

        # Universe-empty fallback: runs created before PR 2's
        # universe-backfill fix have ``universe=[]`` in the JSONB column.
        # We derive symbols from any source we have — signal_log first
        # (richest), trades second (covers runs with closed positions but
        # no funnel data). Only truly-empty runs leave universe as [],
        # which the page-level stale-run banner explains.
        universe = _to_universe(run.universe)
        if not universe and (sig_rows or trade_rows):
            universe = await _derive_universe_from_logs(session, run_id)

    # Group signal-log rows by virtual_time → TickSummary.
    by_tick: dict[datetime, list[BacktestSignalLog]] = defaultdict(list)
    for r in sig_rows:
        by_tick[r.virtual_time].append(r)
    ticks = [
        _aggregate_tick_summary(t, rows) for t, rows in sorted(by_tick.items())
    ]

    return SessionSkeleton(
        metadata=_to_run_metadata(run),
        universe=universe,
        config_snapshot=run.config_snapshot or {},
        ticks=ticks,
        trades=[_to_trade_record(t) for t in trade_rows],
    )


async def _derive_universe_from_logs(
    session, run_id: uuid.UUID,
) -> list[UniverseEntry]:
    """Reconstruct the universe from distinct underlyings touched by the run.

    Unions two sources:
      * ``backtest_signal_log.underlying_id`` — every arm that signalled
      * ``backtest_trades.underlying_id`` — every position opened

    Most runs have both; old runs (pre-PR 1) often have neither, in which
    case this function returns []. The instruments join supplies the
    symbol + name. Sorted by symbol for stable display order.
    """
    q = (
        select(
            Instrument.id, Instrument.symbol, Instrument.company_name,
        )
        .where(
            Instrument.id.in_(
                select(BacktestSignalLog.underlying_id)
                .where(BacktestSignalLog.backtest_run_id == run_id)
                .union(
                    select(BacktestTrade.underlying_id)
                    .where(BacktestTrade.backtest_run_id == run_id)
                )
            )
        )
        .order_by(Instrument.symbol)
    )
    rows = (await session.execute(q)).all()
    return [
        UniverseEntry(instrument_id=r[0], symbol=r[1], name=r[2])
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 3. load_underlying_timeline — price + VIX overlay
# ---------------------------------------------------------------------------

async def load_underlying_timeline(
    run_id: uuid.UUID,
    symbol: str,
) -> UnderlyingTimeline | None:
    """Return all 3-min bars + VIX ticks for one symbol on the run's date.

    Returns None when the symbol isn't recognised as an active F&O
    instrument (defensive — we don't need a backtest_runs join because
    a symbol that wasn't in the universe still has price data).
    """
    async with session_scope() as session:
        run = await session.get(BacktestRun, run_id)
        if run is None:
            return None

        instr = (await session.execute(
            select(Instrument).where(Instrument.symbol == symbol)
        )).scalar_one_or_none()
        if instr is None:
            return None

        open_utc, close_utc = _utc_session_window(run.backtest_date)

        bars_q = (
            select(PriceIntraday)
            .where(
                and_(
                    PriceIntraday.instrument_id == instr.id,
                    PriceIntraday.timestamp >= open_utc,
                    PriceIntraday.timestamp <= close_utc,
                )
            )
            .order_by(asc(PriceIntraday.timestamp))
        )
        bar_rows = (await session.execute(bars_q)).scalars().all()

        vix_q = (
            select(VIXTick)
            .where(
                and_(
                    VIXTick.timestamp >= open_utc,
                    VIXTick.timestamp <= close_utc,
                )
            )
            .order_by(asc(VIXTick.timestamp))
        )
        vix_rows = (await session.execute(vix_q)).scalars().all()

    return UnderlyingTimeline(
        underlying_id=instr.id,
        symbol=symbol,
        bars=[
            PriceBar(
                timestamp=b.timestamp,
                open=float(b.open),
                high=float(b.high),
                low=float(b.low),
                close=float(b.close),
                volume=int(b.volume),
            )
            for b in bar_rows
        ],
        vix=[
            VIXBar(
                timestamp=v.timestamp,
                value=float(v.vix_value),
                regime=str(v.regime),
            )
            for v in vix_rows
        ],
    )


# ---------------------------------------------------------------------------
# 4. load_tick_state — the Tick Inspector waterfall payload
# ---------------------------------------------------------------------------

async def load_tick_state(
    run_id: uuid.UUID,
    virtual_time: datetime,
    *,
    symbol: str | None = None,
) -> TickState | None:
    """Return everything the Tick Inspector needs for one (time × symbol).

    When ``symbol`` is provided, the focus underlying's ``FeatureBundle``
    is recomputed at ``virtual_time`` via ``BacktestFeatureStore``. Pass
    None to skip the recompute (cheap mode — only loads signal-log rows).

    Returns None when the run_id doesn't exist. When no signal-log rows
    exist at this virtual_time (e.g. a tick where every primitive returned
    None), an empty ``TickState`` is returned with empty lists — distinct
    from the missing-run case.
    """
    async with session_scope() as session:
        run = await session.get(BacktestRun, run_id)
        if run is None:
            return None

        sig_q = select(BacktestSignalLog).where(
            and_(
                BacktestSignalLog.backtest_run_id == run_id,
                BacktestSignalLog.virtual_time == virtual_time,
            )
        )
        if symbol is not None:
            sig_q = sig_q.where(BacktestSignalLog.symbol == symbol)
        sig_rows = (await session.execute(sig_q)).scalars().all()

        # Resolve underlying_id for the focus symbol. Prefer the universe
        # JSONB (the actual instrument the run targeted); fall back to
        # the instruments table so the inspector still works for symbols
        # that weren't in the universe (e.g. when investigating a missed
        # gainer that wasn't selected).
        underlying_id = None
        if symbol is not None:
            for u in run.universe or []:
                if u.get("symbol") == symbol:
                    try:
                        underlying_id = (
                            u["id"] if isinstance(u["id"], uuid.UUID)
                            else uuid.UUID(str(u["id"]))
                        )
                    except (ValueError, TypeError):
                        underlying_id = None
                    break
            if underlying_id is None:
                instr = (await session.execute(
                    select(Instrument).where(Instrument.symbol == symbol)
                )).scalar_one_or_none()
                if instr is not None:
                    underlying_id = instr.id

    # Recompute the focus FeatureBundle outside the session — the store
    # opens its own. Best-effort; missing data → None.
    feature_bundle: FeatureBundle | None = None
    if symbol is not None and underlying_id is not None:
        try:
            store = BacktestFeatureStore(trading_date=run.backtest_date)
            feature_bundle = await store.get(underlying_id, virtual_time)
        except Exception as exc:
            logger.warning(
                f"load_tick_state: feature recompute failed for "
                f"{symbol} @ {virtual_time}: {exc!r}"
            )

    primitive_signals = [_to_primitive_signal(r) for r in sig_rows]
    bandit_view = _reconstruct_tournament(sig_rows)
    sizer_view, chosen_arm = _extract_sizer_outcome(sig_rows)

    return TickState(
        virtual_time=virtual_time,
        symbol=symbol or "",
        underlying_id=underlying_id,
        feature_bundle=feature_bundle,
        primitive_signals=primitive_signals,
        bandit_tournament=bandit_view,
        sizer_outcome=sizer_view,
        chosen_arm_id=chosen_arm,
    )


# ---------------------------------------------------------------------------
# 5. load_arm_history — heatmap rail
# ---------------------------------------------------------------------------

async def load_arm_history(
    run_id: uuid.UUID,
    arm_id: str,
) -> ArmHistory | None:
    """Return per-tick trajectory for one arm.

    Returns None when the arm never signalled in this run (vs. an empty-
    history result, which indicates the arm exists but had no rows).
    """
    async with session_scope() as session:
        run = await session.get(BacktestRun, run_id)
        if run is None:
            return None
        q = (
            select(BacktestSignalLog)
            .where(
                and_(
                    BacktestSignalLog.backtest_run_id == run_id,
                    BacktestSignalLog.arm_id == arm_id,
                )
            )
            .order_by(asc(BacktestSignalLog.virtual_time))
        )
        rows = (await session.execute(q)).scalars().all()

    if not rows:
        return None

    # arm_id schema is "{symbol}_{primitive_name}" — split on the last "_"
    # so symbols containing underscores survive intact.
    head = rows[0]
    primitive_name = head.primitive_name
    symbol = head.symbol

    ticks: list[ArmTickState] = []
    for r in rows:
        ba = (r.bandit_trace or {}).get("this_arm") if r.bandit_trace else None
        ticks.append(
            ArmTickState(
                virtual_time=r.virtual_time,
                rejection_reason=r.rejection_reason,
                strength=float(r.strength),
                posterior_mean=(
                    float(r.posterior_mean)
                    if r.posterior_mean is not None
                    else None
                ),
                sampled_mean=(
                    float(ba["sampled_mean"])
                    if ba and "sampled_mean" in ba else None
                ),
                signal_strength=(
                    float(ba["signal_strength"])
                    if ba and "signal_strength" in ba else None
                ),
                score=(
                    float(ba["score"]) if ba and "score" in ba else None
                ),
                bandit_selected=bool(r.bandit_selected),
                lots_sized=r.lots_sized,
            )
        )
    return ArmHistory(
        arm_id=arm_id,
        primitive_name=primitive_name,
        symbol=symbol,
        ticks=ticks,
    )


# ---------------------------------------------------------------------------
# 5b. load_arm_matrix — multi-arm trajectories for the heatmap
# ---------------------------------------------------------------------------

async def load_arm_matrix(run_id: uuid.UUID) -> list[ArmHistory]:
    """Return per-tick trajectories for every arm that signalled in the run.

    One query (instead of N ``load_arm_history`` round-trips) returns every
    signal-log row; we group in Python by ``arm_id``. Arms that never
    signalled are omitted — the heatmap should only show rows that have
    something to colour.

    Returns an empty list when the run_id is unknown or when no arm fired.
    Sorted by ``arm_id`` for stable display order across reruns.
    """
    async with session_scope() as session:
        run = await session.get(BacktestRun, run_id)
        if run is None:
            return []
        q = (
            select(BacktestSignalLog)
            .where(BacktestSignalLog.backtest_run_id == run_id)
            .order_by(asc(BacktestSignalLog.virtual_time))
        )
        rows = (await session.execute(q)).scalars().all()

    by_arm: dict[str, list[BacktestSignalLog]] = defaultdict(list)
    for r in rows:
        by_arm[r.arm_id].append(r)

    out: list[ArmHistory] = []
    for arm_id in sorted(by_arm.keys()):
        arm_rows = by_arm[arm_id]
        head = arm_rows[0]
        ticks: list[ArmTickState] = []
        for r in arm_rows:
            ba = (r.bandit_trace or {}).get("this_arm") if r.bandit_trace else None
            ticks.append(
                ArmTickState(
                    virtual_time=r.virtual_time,
                    rejection_reason=r.rejection_reason,
                    strength=float(r.strength),
                    posterior_mean=(
                        float(r.posterior_mean)
                        if r.posterior_mean is not None
                        else None
                    ),
                    sampled_mean=(
                        float(ba["sampled_mean"])
                        if ba and "sampled_mean" in ba else None
                    ),
                    signal_strength=(
                        float(ba["signal_strength"])
                        if ba and "signal_strength" in ba else None
                    ),
                    score=(
                        float(ba["score"]) if ba and "score" in ba else None
                    ),
                    bandit_selected=bool(r.bandit_selected),
                    lots_sized=r.lots_sized,
                )
            )
        out.append(
            ArmHistory(
                arm_id=arm_id,
                primitive_name=head.primitive_name,
                symbol=head.symbol,
                ticks=ticks,
            )
        )
    return out


# ---------------------------------------------------------------------------
# 6. load_tick_diff — bottom diff strip
# ---------------------------------------------------------------------------

async def load_tick_diff(
    run_id: uuid.UUID,
    t1: datetime,
    t2: datetime,
    symbol: str,
) -> TickDiff | None:
    """Return per-feature deltas for ``symbol`` between ``t1`` and ``t2``.

    Recomputes both ``FeatureBundle``s via the backtest store (no lookahead;
    the store enforces ``timestamp <= virtual_time`` on every read). Returns
    None when the run_id or symbol can't be resolved.
    """
    async with session_scope() as session:
        run = await session.get(BacktestRun, run_id)
        if run is None:
            return None
        instr = (await session.execute(
            select(Instrument).where(Instrument.symbol == symbol)
        )).scalar_one_or_none()
        if instr is None:
            return None

    store = BacktestFeatureStore(trading_date=run.backtest_date)
    try:
        b1 = await store.get(instr.id, t1)
        b2 = await store.get(instr.id, t2)
    except Exception as exc:
        logger.warning(
            f"load_tick_diff: feature recompute failed "
            f"({symbol} {t1}->{t2}): {exc!r}"
        )
        return None

    deltas = _compute_feature_deltas(b1, b2)
    regime_change: tuple[str, str] | None = None
    if b1 is not None and b2 is not None and b1.vix_regime != b2.vix_regime:
        regime_change = (b1.vix_regime, b2.vix_regime)

    return TickDiff(
        symbol=symbol,
        t1=t1,
        t2=t2,
        deltas=deltas,
        regime_change=regime_change,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _utc_session_window(trading_date: date) -> tuple[datetime, datetime]:
    """Return (session_open_utc, session_close_utc) for an IST trading day."""
    open_ist = _IST.localize(datetime.combine(trading_date, time(9, 15)))
    close_ist = _IST.localize(datetime.combine(trading_date, time(15, 30)))
    return open_ist.astimezone(timezone.utc), close_ist.astimezone(timezone.utc)


# Numeric FeatureBundle fields the diff strip ranks. Listed explicitly so a
# new field added to the bundle later doesn't silently appear in the strip
# (better to extend deliberately than to surface unfamiliar deltas).
_DIFFABLE_FIELDS: tuple[str, ...] = (
    "underlying_ltp",
    "underlying_volume_3min",
    "vwap_today",
    "realized_vol_3min",
    "realized_vol_30min",
    "atm_iv",
    "atm_oi",
    "bid_volume_3min_change",
    "ask_volume_3min_change",
    "bb_width",
    "vix_value",
)


def _to_float(value: Any) -> float | None:
    """Coerce an arbitrary numeric (Decimal/int/float/None) → float | None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _compute_feature_deltas(
    b1: FeatureBundle | None,
    b2: FeatureBundle | None,
) -> list[FeatureDelta]:
    """Compute per-field deltas. Either bundle may be None (missing data)."""
    out: list[FeatureDelta] = []
    for name in _DIFFABLE_FIELDS:
        v1 = _to_float(getattr(b1, name, None)) if b1 is not None else None
        v2 = _to_float(getattr(b2, name, None)) if b2 is not None else None
        delta = (v2 - v1) if (v1 is not None and v2 is not None) else None
        pct = (delta / v1) if (delta is not None and v1) else None
        out.append(
            FeatureDelta(
                name=name,
                value_t1=v1,
                value_t2=v2,
                delta=delta,
                pct_change=pct,
            )
        )
    return out
