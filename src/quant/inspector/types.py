"""Typed read-API surface for the Decision Inspector (PR 2).

These dataclasses define the *contract* between the reader (this package)
and the Streamlit UI (PR 3+). UI code never touches SQLAlchemy models or
JSONB blobs directly — it consumes these types.

Design rules:
  * Frozen dataclasses (immutable, hashable) — Streamlit caches by hash, so
    immutability matters.
  * No SQLAlchemy types in fields — pure stdlib + ``FeatureBundle`` from the
    quant layer.
  * Trace ``dict`` payloads (primitive_trace / sizer_trace / per-arm bandit
    slice) are passed through *as-is* from JSONB. Their keys are documented
    in ``database/migrations/2026_05_10_signal_log_traces.sql`` and
    ``src/quant/recorder.py``.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime

from src.quant.feature_store import FeatureBundle


# ---------------------------------------------------------------------------
# Run-level
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunMetadata:
    """One row from ``backtest_runs`` — used by the run-picker dropdown.

    Lightweight (no per-tick aggregates) so the picker stays fast even when
    the user has hundreds of runs across many backtest days.
    """

    run_id: uuid.UUID
    portfolio_id: uuid.UUID
    backtest_date: date
    started_at: datetime
    completed_at: datetime | None
    starting_nav: float
    final_nav: float | None
    pnl_pct: float | None
    trade_count: int | None
    bandit_seed: int


@dataclass(frozen=True)
class UniverseEntry:
    """One symbol in a run's selected universe."""

    instrument_id: uuid.UUID
    symbol: str
    name: str | None


@dataclass(frozen=True)
class TickSummary:
    """Per-tick aggregate counts — drives the scrubber's tick markers.

    All fields derived from ``backtest_signal_log`` rows aggregated by
    ``virtual_time``. ``n_signals_total`` includes weak ones (the funnel
    captures every primitive output, not just the strong-passing ones).
    """

    virtual_time: datetime
    n_signals_total: int
    n_signals_strong: int       # rejection_reason != 'weak_signal'
    n_opened: int
    n_lost_bandit: int
    n_sized_zero: int
    n_cooloff: int
    n_kill_switch: int
    n_capacity_full: int
    n_warmup: int


@dataclass(frozen=True)
class TradeRecord:
    """One ``backtest_trades`` row, surfaced for the timeline + outcome view."""

    trade_id: uuid.UUID
    arm_id: str
    primitive_name: str
    underlying_id: uuid.UUID
    direction: str
    entry_at: datetime
    exit_at: datetime | None
    entry_premium_net: float
    exit_premium_net: float | None
    realized_pnl: float | None
    lots: int
    exit_reason: str | None


@dataclass(frozen=True)
class SessionSkeleton:
    """Everything the scrubber + summary panels need for one run.

    Heavy enough to hold all per-tick markers + trade markers, but does
    NOT include per-tick signal-log rows (those load on demand via
    ``load_tick_state``).
    """

    metadata: RunMetadata
    universe: list[UniverseEntry]
    config_snapshot: dict
    ticks: list[TickSummary]
    trades: list[TradeRecord]


# ---------------------------------------------------------------------------
# Underlying timeline (price + VIX)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PriceBar:
    """One 3-min OHLCV bar from ``price_intraday``."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class VIXBar:
    """One VIX observation from ``vix_ticks``."""

    timestamp: datetime
    value: float
    regime: str


@dataclass(frozen=True)
class UnderlyingTimeline:
    """Price + VIX series for one focus symbol over the trading day.

    Drives the scrubber's market overlay. VIX is the same series for every
    symbol in a run (single underlying), but bundled here for one-call
    convenience.
    """

    underlying_id: uuid.UUID
    symbol: str
    bars: list[PriceBar]
    vix: list[VIXBar]


# ---------------------------------------------------------------------------
# Tick-state (the depth view — one (virtual_time × symbol))
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PrimitiveSignalView:
    """One primitive's output at a tick — populates the formula card."""

    arm_id: str
    primitive_name: str
    direction: str
    strength: float
    rejection_reason: str
    posterior_mean: float | None
    bandit_selected: bool
    lots_sized: int | None
    primitive_trace: dict | None      # {name, inputs, intermediates, formula}


@dataclass(frozen=True)
class BanditTournamentView:
    """Reconstructed bandit tournament for one tick.

    Built by aggregating the per-arm slices stored on each row's
    ``bandit_trace.this_arm``. Empty when no arms reached the bandit
    (warmup / kill_switch / capacity_full / all-cooloff ticks).
    """

    algo: str                                # 'lints' | 'thompson'
    context_vector: list[float] | None       # only for lints
    context_dims: list[str] | None           # only for lints
    arms: dict[str, dict]                    # arm_id -> per-arm payload
    selected_arm_id: str | None
    n_competitors: int


@dataclass(frozen=True)
class SizerOutcomeView:
    """Full Kelly cascade for the chosen arm — None on ticks with no entry."""

    final_lots: int
    blocking_step: str | None
    inputs: dict
    constants: dict
    cascade: list[dict]


@dataclass(frozen=True)
class TickState:
    """Everything the Tick Inspector waterfall needs for one moment.

    Either the focus-symbol's FeatureBundle is recomputed (via
    BacktestFeatureStore) or — if the underlying has no data at that
    virtual_time — ``feature_bundle`` is None and the inputs panel renders
    a placeholder. The other fields still populate (signals etc. came from
    the persisted log, not a live recompute).
    """

    virtual_time: datetime
    symbol: str
    underlying_id: uuid.UUID | None         # None if symbol unknown to the run
    feature_bundle: FeatureBundle | None
    primitive_signals: list[PrimitiveSignalView]
    bandit_tournament: BanditTournamentView | None
    sizer_outcome: SizerOutcomeView | None
    chosen_arm_id: str | None


# ---------------------------------------------------------------------------
# Per-arm history (heatmap source)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArmTickState:
    """One tick's slice of one arm's trajectory."""

    virtual_time: datetime
    rejection_reason: str
    strength: float
    posterior_mean: float | None
    sampled_mean: float | None              # from bandit_trace.this_arm
    signal_strength: float | None           # from bandit_trace.this_arm
    score: float | None                     # from bandit_trace.this_arm
    bandit_selected: bool
    lots_sized: int | None


@dataclass(frozen=True)
class ArmHistory:
    """Per-tick trajectory for one arm — feeds the right-rail heatmap.

    ``ticks`` is sorted by ``virtual_time`` ascending. Empty list when the
    arm never signalled in this run.
    """

    arm_id: str
    primitive_name: str
    symbol: str
    ticks: list[ArmTickState]


# ---------------------------------------------------------------------------
# Diff strip (T vs T-1)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FeatureDelta:
    """One feature's change between two ticks."""

    name: str
    value_t1: float | None
    value_t2: float | None
    delta: float | None
    pct_change: float | None        # delta / value_t1; None when t1 is 0/None


@dataclass(frozen=True)
class TickDiff:
    """Per-feature deltas between two ticks for one focus symbol.

    Only fields that meaningfully delta (numeric, present in both bundles)
    are included; categorical/text fields like ``vix_regime`` are emitted
    only when they changed.
    """

    symbol: str
    t1: datetime
    t2: datetime
    deltas: list[FeatureDelta]
    regime_change: tuple[str, str] | None   # (old, new) when vix_regime flipped
