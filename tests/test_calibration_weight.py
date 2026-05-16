"""Tests for the calibration row-weight composition.

This is the load-bearing reliability discount that decides how much each
training row counts in the Platt / isotonic fit. The semantics matter
operationally (over-weighting noisy backfill rows would degrade live
calibration once Stage 1 data lands in the pool), so the stacking rules
are pinned down here.
"""
from __future__ import annotations

import pytest

from src.fno.calibration import (
    _COUNTERFACTUAL_WEIGHT_MULT,
    _IPS_WEIGHT_CLIP,
    _compose_calibration_weight,
)


def test_live_traded_row_gets_no_discount() -> None:
    w = _compose_calibration_weight(
        propensity=0.5,
        outcome_class="traded",
        propensity_source="live",
    )
    # 1/0.5 = 2.0, no multipliers
    assert w == pytest.approx(2.0)


def test_live_counterfactual_gets_single_discount() -> None:
    w = _compose_calibration_weight(
        propensity=0.5,
        outcome_class="counterfactual",
        propensity_source="live",
    )
    # 2.0 × 0.3 = 0.6
    assert w == pytest.approx(2.0 * _COUNTERFACTUAL_WEIGHT_MULT)


def test_backfill_counterfactual_stacks_both_discounts() -> None:
    # Stage 1 backfill row: imputed propensity AND counterfactual outcome.
    # Plan §3.2 + §7.2: the two flags index independent reliability
    # concerns, so they compound.
    w = _compose_calibration_weight(
        propensity=0.5,
        outcome_class="counterfactual",
        propensity_source="imputed",
    )
    expected = 2.0 * _COUNTERFACTUAL_WEIGHT_MULT * _COUNTERFACTUAL_WEIGHT_MULT
    assert w == pytest.approx(expected)


def test_imputed_traded_row_gets_only_imputed_discount() -> None:
    # Hypothetical (shouldn't occur in practice — backfill rows are
    # always counterfactual) but verifies the flags are independent.
    w = _compose_calibration_weight(
        propensity=0.5,
        outcome_class="traded",
        propensity_source="imputed",
    )
    assert w == pytest.approx(2.0 * _COUNTERFACTUAL_WEIGHT_MULT)


def test_counterfactual_eod_and_intraday_treated_as_counterfactual() -> None:
    # Stage 2 taxonomy variants must trigger the same discount.
    for oc in ("counterfactual_eod", "counterfactual_intraday"):
        w = _compose_calibration_weight(
            propensity=1.0,
            outcome_class=oc,
            propensity_source="live",
        )
        assert w == pytest.approx(_COUNTERFACTUAL_WEIGHT_MULT), f"missed: {oc}"


def test_missing_propensity_treated_as_one() -> None:
    w = _compose_calibration_weight(
        propensity=None,
        outcome_class="traded",
        propensity_source="live",
    )
    # 1/1 = 1, clipped (1.0 is within [0.1, 10])
    assert w == pytest.approx(1.0)


def test_tiny_propensity_clipped_to_upper_bound() -> None:
    w = _compose_calibration_weight(
        propensity=0.001,
        outcome_class="traded",
        propensity_source="live",
    )
    assert w == pytest.approx(_IPS_WEIGHT_CLIP[1])


def test_huge_propensity_clipped_to_lower_bound() -> None:
    w = _compose_calibration_weight(
        propensity=100.0,
        outcome_class="traded",
        propensity_source="live",
    )
    assert w == pytest.approx(_IPS_WEIGHT_CLIP[0])


def test_unknown_propensity_source_is_no_op() -> None:
    # Legacy rows have propensity_source='unknown' (the migration default).
    # They must NOT receive the imputed discount.
    w = _compose_calibration_weight(
        propensity=0.5,
        outcome_class="traded",
        propensity_source="unknown",
    )
    assert w == pytest.approx(2.0)
