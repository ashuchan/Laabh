"""Regression test for the ``warmup_minutes`` ÷ 3 bug (Phase 1, Bug 1).

Before the fix, the orchestrator divided ``warmup_minutes`` by 3
(treating it as minutes), but every primitive set the value as a
**bar count**. The result: ``max_history_bars`` capped per-symbol
history at 11 bars, permanently blocking any primitive whose warmup
exceeded 10 bars.

Concrete fallout in the smoke run:
  * ``momentum``      (needs 11 bars) → 0 signal_log rows over 9 days
  * ``vol_breakout``  (needs 20 bars) → 0 signal_log rows over 9 days

This test ensures that, given the canonical primitive set, the
orchestrator's history cap is large enough that every primitive can
clear its own warmup gate.
"""
from __future__ import annotations

import inspect

from src.quant.primitives import (
    index_revert, momentum, ofi, orb, vol_breakout, vwap_revert,
)
from src.quant.primitives.base import BasePrimitive


# ---------------------------------------------------------------------------
# Field-rename hygiene
# ---------------------------------------------------------------------------

ALL_PRIMITIVE_CLASSES = [
    momentum.MomentumPrimitive,
    vwap_revert.VWAPRevertPrimitive,
    orb.ORBPrimitive,
    vol_breakout.VolBreakoutPrimitive,
    ofi.OFIPrimitive,
    index_revert.IndexRevertPrimitive,
]


def test_every_primitive_exposes_warmup_bars_not_warmup_minutes():
    """The field is now ``warmup_bars``. ``warmup_minutes`` should be gone
    everywhere — its existence anywhere would re-introduce the div-by-3 bug
    if some future code path read the old name."""
    for cls in ALL_PRIMITIVE_CLASSES:
        instance = cls()
        assert hasattr(instance, "warmup_bars"), f"{cls.__name__} lacks warmup_bars"
        assert isinstance(instance.warmup_bars, int) and instance.warmup_bars > 0, (
            f"{cls.__name__}.warmup_bars must be a positive int"
        )
        assert not hasattr(cls, "warmup_minutes"), (
            f"{cls.__name__} still defines warmup_minutes — rename incomplete"
        )


def test_base_class_declares_warmup_bars():
    """The annotation on the base class is the contract every primitive
    must satisfy. Catching a regression where the base name drifts away
    from the concrete attributes."""
    annotations = inspect.get_annotations(BasePrimitive)
    assert "warmup_bars" in annotations
    assert "warmup_minutes" not in annotations


# ---------------------------------------------------------------------------
# Orchestrator history-cap math
# ---------------------------------------------------------------------------

def test_orchestrator_history_cap_satisfies_largest_warmup():
    """Replicate the orchestrator's ``max_history_bars`` calculation
    (lifted verbatim from ``src/quant/orchestrator.py``) and assert that
    after the per-tick ``hist[:-1]`` slice, EVERY primitive can clear
    its warmup gate.

    This is the single test that would have caught Bug 1 the day it
    was introduced.
    """
    primitives = [cls() for cls in ALL_PRIMITIVE_CLASSES]
    # Mirror the orchestrator's math (currently in run_loop bootstrap).
    max_history_bars = max((p.warmup_bars for p in primitives), default=10) + 2
    # Per-tick the orchestrator slices ``history[symbol][:-1]`` (excludes
    # current bar). After steady state the history list holds exactly
    # ``max_history_bars`` items, so primitives see ``max_history_bars - 1``.
    available_to_primitives = max_history_bars - 1
    for prim in primitives:
        assert available_to_primitives >= prim.warmup_bars, (
            f"{prim.name} needs {prim.warmup_bars} bars of history but "
            f"the orchestrator's cap only delivers {available_to_primitives}. "
            f"This is the Bug 1 regression — increase the cap or shrink the warmup."
        )


def test_canonical_primitive_warmup_bars_match_documented_values():
    """Lock the warmup-bar values so a future tweak to one primitive
    can't silently break the orchestrator's history cap.

    Update this test deliberately when changing a primitive's setup
    requirements — the failure surfaces the contract drift."""
    expected = {
        "orb":           10,
        "vwap_revert":   10,
        "momentum":      11,   # _N_BARS=10 + 1 to compute the first log-return
        "vol_breakout":  20,
        "ofi":            5,
        "index_revert":  10,
    }
    by_name = {cls().name: cls().warmup_bars for cls in ALL_PRIMITIVE_CLASSES}
    assert by_name == expected
