"""OrchestratorContext — bundles the I/O dependencies the orchestrator needs.

Each field is an *abstraction*; concrete live or backtest implementations are
plugged in by the caller. The orchestrator never imports concrete classes;
its only knowledge of mode is the set of injected dependencies.

Construction helpers:
  * ``live()`` — returns a fully-wired live context. Used as the default in
    ``run_loop`` so existing call sites need no change.
  * Backtest contexts are constructed by ``BacktestRunner`` (Task 10).

SOLID notes:
  * DIP — orchestrator depends on these abstractions, not concrete classes.
  * ISP — only the four roles the orchestrator actually uses (clock, feature-
    getter, universe selector, trade recorder) are exposed.
  * OCP — adding a new mode (e.g. simulation harness) means writing new
    implementations and a new ``Context.<mode>`` factory; orchestrator
    untouched.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, Literal

from src.quant.clock import Clock, LiveClock
from src.quant.feature_store import FeatureBundle
from src.quant.recorder import LiveTradeRecorder, TradeRecorder
from src.quant.universe import LLMUniverseSelector, UniverseSelector


# Type alias for the per-tick feature read.
# Live impl: ``src.quant.feature_store.get`` (module function).
# Backtest impl: ``BacktestFeatureStore.get`` (bound method).
FeatureGetter = Callable[[uuid.UUID, datetime], Awaitable["FeatureBundle | None"]]


@dataclass
class OrchestratorContext:
    """Injectable I/O for the orchestrator's main loop."""

    mode: Literal["live", "backtest"]
    clock: Clock
    feature_getter: FeatureGetter
    universe_selector: UniverseSelector
    recorder: TradeRecorder
    # Notifier is optional — backtest sets it to None / no-op.
    notify: Callable[[str], Awaitable[None]] | None = None
    # When set, the orchestrator uses this value as the day's starting NAV
    # instead of querying ``portfolios.current_cash + invested_value``.
    # Required for backtest compounding: the runner threads each day's
    # final NAV into the next day via this field. Live mode leaves it None.
    nav_override: float | None = None
    # Phase-4 fix: when set, the orchestrator builds its primitives + arm
    # universe from this list instead of ``settings.quant_primitives_list``.
    # Used by the BacktestRunner to drop primitives that are guaranteed
    # silent under backtest data (OFI needs L1 quotes; index_revert needs
    # the universe to include indices). Live callers leave it None →
    # settings drive everything as before.
    primitives_override: list[str] | None = None

    @classmethod
    def live(cls) -> "OrchestratorContext":
        """Default live context. Wires the existing live implementations.

        Used by ``run_loop`` when the caller passes no ``ctx``, preserving
        backwards compatibility with every existing call site.
        """
        from src.quant import feature_store as live_feature_store

        return cls(
            mode="live",
            clock=LiveClock(),
            feature_getter=live_feature_store.get,
            universe_selector=LLMUniverseSelector(),
            recorder=LiveTradeRecorder(),
            notify=None,  # live notifications are still routed through
                          # existing notification_service from inside the
                          # orchestrator's helpers; this field is reserved
                          # for future centralisation.
        )
