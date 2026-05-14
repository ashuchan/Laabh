"""Coverage for src.fno.llm_agreement.synthetic_v10_label.

The agreement-matrix DB path (``compute_agreement``) is exercised via
manual smoke + the dashboard; this file pins the synthetic-label rule
that decides what v10 outputs become PROCEED/HEDGE/SKIP for matrix
counting (plan §1.4).
"""
from __future__ import annotations

from src.fno.llm_agreement import synthetic_v10_label


def test_synthetic_proceed_high_conviction_and_durability() -> None:
    assert synthetic_v10_label(directional_conviction=0.6, thesis_durability=0.7) == "PROCEED"


def test_synthetic_proceed_negative_conviction_same_magnitude() -> None:
    """Sign carries direction; magnitude carries conviction strength."""
    assert synthetic_v10_label(directional_conviction=-0.7, thesis_durability=0.6) == "PROCEED"


def test_synthetic_hedge_strong_conviction_short_durability() -> None:
    """A strong but short-lived view downgrades to HEDGE."""
    assert synthetic_v10_label(directional_conviction=0.6, thesis_durability=0.2) == "HEDGE"


def test_synthetic_hedge_mid_conviction_long_durability() -> None:
    """Conviction below the PROCEED bar but above the HEDGE floor."""
    assert synthetic_v10_label(directional_conviction=0.25, thesis_durability=0.6) == "HEDGE"


def test_synthetic_skip_weak_conviction() -> None:
    """Below the HEDGE floor — no actionable signal."""
    assert synthetic_v10_label(directional_conviction=0.1, thesis_durability=0.5) == "SKIP"


def test_synthetic_skip_missing_inputs() -> None:
    assert synthetic_v10_label(directional_conviction=None, thesis_durability=0.5) == "SKIP"
    assert synthetic_v10_label(directional_conviction=0.5, thesis_durability=None) == "SKIP"


def test_synthetic_proceed_boundary_conviction() -> None:
    """Exactly at threshold: |0.4| > 0.4 is false → HEDGE, NOT PROCEED.

    Pins the strict-greater-than semantics so a future refactor that
    accidentally relaxes to >= doesn't silently widen the PROCEED band.
    """
    assert synthetic_v10_label(directional_conviction=0.4, thesis_durability=0.6) == "HEDGE"


def test_synthetic_proceed_just_above_boundary() -> None:
    assert synthetic_v10_label(directional_conviction=0.41, thesis_durability=0.51) == "PROCEED"
