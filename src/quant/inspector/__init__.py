"""Decision Inspector — read-only view layer over PR 1's signal-log data.

PR 2 ships the *contract* the Streamlit UI (PR 3+) consumes. UI code never
touches SQLAlchemy models or JSONB blobs — it only sees the typed dataclasses
in ``src.quant.inspector.types`` returned by the readers in
``src.quant.inspector.reader``.

Public surface:

  * Readers      — six async functions, each returning a typed dataclass.
  * Types        — frozen dataclasses; safe to hash for Streamlit caches.

Add new readers/types here, not in ad-hoc modules — keeps the contract
discoverable from one import site.
"""
from src.quant.inspector.reader import (
    list_runs,
    load_arm_history,
    load_arm_matrix,
    load_session_skeleton,
    load_tick_diff,
    load_tick_state,
    load_underlying_timeline,
)
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

__all__ = [
    # Readers
    "list_runs",
    "load_arm_history",
    "load_arm_matrix",
    "load_session_skeleton",
    "load_tick_diff",
    "load_tick_state",
    "load_underlying_timeline",
    # Types — re-exported so callers can `from src.quant.inspector import X`
    "ArmHistory",
    "ArmTickState",
    "BanditTournamentView",
    "FeatureDelta",
    "PriceBar",
    "PrimitiveSignalView",
    "RunMetadata",
    "SessionSkeleton",
    "SizerOutcomeView",
    "TickDiff",
    "TickState",
    "TickSummary",
    "TradeRecord",
    "UnderlyingTimeline",
    "UniverseEntry",
    "VIXBar",
]
