"""Tests for src.collectors.sentiment_collector.

Pure-function coverage on the scoring helpers, plus mock-based coverage
of the degraded-path branches (missing legs, all-None horizons, fallback
weights). Skips full DB integration — the I/O helpers are exercised via
patches.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.collectors.sentiment_collector import (
    _trading_day_gap,
    combine_horizon,
    combine_score,
    decayed_weights,
    score_breadth_leg,
    score_index_leg,
    score_vix,
)


# ---------------------------------------------------------------------------
# score_vix — piecewise-linear inverse map
# ---------------------------------------------------------------------------

def test_score_vix_low_saturates() -> None:
    assert score_vix(8.0) == 8.0
    assert score_vix(12.0) == 8.0


def test_score_vix_anchors_match() -> None:
    # The function uses linear interpolation between (12, 8) → (15, 6) → ...
    assert score_vix(15.0) == 6.0
    assert score_vix(18.0) == 4.0
    assert score_vix(22.0) == 2.0


def test_score_vix_high_saturates() -> None:
    assert score_vix(30.0) == 0.5
    assert score_vix(45.0) == 0.5


def test_score_vix_interpolates_between_anchors() -> None:
    # Halfway between 12 (→ 8.0) and 15 (→ 6.0) → 7.0
    assert score_vix(13.5) == 7.0


# ---------------------------------------------------------------------------
# score_index_leg / score_breadth_leg
# ---------------------------------------------------------------------------

def test_score_index_leg_neutral() -> None:
    assert score_index_leg(0.0, 1.0) == 5.0


def test_score_index_leg_saturates_high() -> None:
    # +5% with scale=1.0 → 10.0 (capped)
    assert score_index_leg(5.0, 1.0) == 10.0
    assert score_index_leg(50.0, 1.0) == 10.0


def test_score_index_leg_saturates_low() -> None:
    assert score_index_leg(-5.0, 1.0) == 0.0
    assert score_index_leg(-100.0, 1.0) == 0.0


def test_score_index_leg_1m_calibration() -> None:
    # 1m scale = 0.5 → +10% maps to 10.0, +2% to 6.0
    assert score_index_leg(2.0, 0.5) == 6.0
    assert score_index_leg(10.0, 0.5) == 10.0


def test_score_breadth_leg_full_range() -> None:
    assert score_breadth_leg(0.0) == 0.0
    assert score_breadth_leg(50.0) == 5.0
    assert score_breadth_leg(100.0) == 10.0


# ---------------------------------------------------------------------------
# combine_horizon — graceful per-leg fallback
# ---------------------------------------------------------------------------

def test_combine_horizon_both_present() -> None:
    assert combine_horizon(6.0, 7.0) == 6.5


def test_combine_horizon_only_index() -> None:
    # Single-leg present → return that leg, NOT averaged with neutral 5.0
    assert combine_horizon(6.0, None) == 6.0


def test_combine_horizon_only_breadth() -> None:
    assert combine_horizon(None, 7.0) == 7.0


def test_combine_horizon_both_none() -> None:
    assert combine_horizon(None, None) is None


# ---------------------------------------------------------------------------
# decayed_weights — Monday / post-holiday adjustment
# ---------------------------------------------------------------------------

_BASE = {"vix": 0.20, "trend_1d": 0.30, "trend_1w": 0.25, "trend_1m": 0.25}


def test_decayed_weights_normal_day_renormalises_to_one() -> None:
    w = decayed_weights(_BASE, trading_day_gap=1, stale_1d_decay=0.5)
    assert abs(sum(w.values()) - 1.0) < 1e-3
    # 1d weight should be unchanged at 0.30 (no decay applied)
    assert w["trend_1d"] == pytest.approx(0.30, abs=0.001)


def test_decayed_weights_monday_halves_1d() -> None:
    w = decayed_weights(_BASE, trading_day_gap=3, stale_1d_decay=0.5)
    # 1d goes from 0.30 → 0.15 raw; renormalised across (0.20+0.15+0.25+0.25=0.85)
    expected_1d = 0.15 / 0.85
    assert w["trend_1d"] == pytest.approx(expected_1d, abs=0.001)
    # Other weights are bumped proportionally
    assert w["vix"] > 0.20
    assert w["trend_1w"] > 0.25
    assert abs(sum(w.values()) - 1.0) < 1e-3


def test_decayed_weights_post_holiday_same_as_monday() -> None:
    # Any gap > 1 triggers the same decay
    w_mon = decayed_weights(_BASE, trading_day_gap=3, stale_1d_decay=0.5)
    w_holiday = decayed_weights(_BASE, trading_day_gap=4, stale_1d_decay=0.5)
    assert w_mon == w_holiday


def test_decayed_weights_decay_factor_tunable() -> None:
    # Operator wants an even harsher discount on Mondays — set decay=0.25
    w = decayed_weights(_BASE, trading_day_gap=3, stale_1d_decay=0.25)
    # 1d goes from 0.30 → 0.075
    expected_1d = 0.075 / (0.20 + 0.075 + 0.25 + 0.25)
    assert w["trend_1d"] == pytest.approx(expected_1d, abs=0.001)


# ---------------------------------------------------------------------------
# combine_score — weighted aggregate with skip-and-renormalise
# ---------------------------------------------------------------------------

def test_combine_score_all_present() -> None:
    weights = {"vix": 0.2, "trend_1d": 0.3, "trend_1w": 0.25, "trend_1m": 0.25}
    scores = {"vix": 5.0, "trend_1d": 6.0, "trend_1w": 7.0, "trend_1m": 4.0}
    final, applied = combine_score(weights, scores)
    expected = 0.2 * 5 + 0.3 * 6 + 0.25 * 7 + 0.25 * 4
    assert final == pytest.approx(expected, abs=0.01)
    assert sum(applied.values()) == pytest.approx(1.0, abs=0.01)


def test_combine_score_drops_missing_horizon_and_renormalises() -> None:
    """If a horizon is None, its weight gets redistributed across the others."""
    weights = {"vix": 0.2, "trend_1d": 0.3, "trend_1w": 0.25, "trend_1m": 0.25}
    scores = {"vix": 5.0, "trend_1d": 6.0, "trend_1w": None, "trend_1m": 4.0}
    final, applied = combine_score(weights, scores)
    # Should NOT be compressed toward neutral — 1w simply drops out
    assert "trend_1w" not in applied
    assert sum(applied.values()) == pytest.approx(1.0, abs=0.01)
    expected = (0.2 * 5 + 0.3 * 6 + 0.25 * 4) / (0.2 + 0.3 + 0.25)
    assert final == pytest.approx(expected, abs=0.01)


def test_combine_score_all_none_returns_none() -> None:
    weights = {"vix": 0.2, "trend_1d": 0.3, "trend_1w": 0.25, "trend_1m": 0.25}
    scores = {k: None for k in weights}
    final, applied = combine_score(weights, scores)
    assert final is None
    assert applied == {}


def test_combine_score_single_component_uses_full_range() -> None:
    """A single non-None component should still hit its actual score, not be
    diluted by the absent neutrals."""
    weights = {"vix": 0.2, "trend_1d": 0.3, "trend_1w": 0.25, "trend_1m": 0.25}
    scores = {"vix": 8.0, "trend_1d": None, "trend_1w": None, "trend_1m": None}
    final, applied = combine_score(weights, scores)
    assert final == 8.0
    assert applied == {"vix": 1.0}


# ---------------------------------------------------------------------------
# _trading_day_gap
# ---------------------------------------------------------------------------

def test_trading_day_gap_normal_day() -> None:
    assert _trading_day_gap(date(2026, 5, 7), date(2026, 5, 8)) == 1


def test_trading_day_gap_monday_after_weekend() -> None:
    # Friday → Monday = 3 calendar days
    assert _trading_day_gap(date(2026, 5, 8), date(2026, 5, 11)) == 3


def test_trading_day_gap_post_holiday() -> None:
    # 4-day gap (e.g., long weekend) — still treated the same as gap > 1
    assert _trading_day_gap(date(2026, 5, 7), date(2026, 5, 11)) == 4


def test_trading_day_gap_unknown_defaults_to_one() -> None:
    # Don't penalise on missing data — assume normal day
    assert _trading_day_gap(None, date(2026, 5, 11)) == 1


def test_trading_day_gap_clamped_to_at_least_one() -> None:
    # Same day shouldn't yield 0 — that would be a weird invariant to invent
    assert _trading_day_gap(date(2026, 5, 8), date(2026, 5, 8)) == 1


# ---------------------------------------------------------------------------
# compute_sentiment — full payload integration with mocks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compute_sentiment_happy_path() -> None:
    """All four components present → score is the weighted average,
    payload structure is well-formed, degraded=False."""
    from src.collectors import sentiment_collector as sc

    vix_comp = {"value": 16.0, "regime": "neutral", "score": 5.5,
                "source": "vix_ticks", "ts": "2026-05-08T03:00:00+00:00"}

    async def fake_index_window(_id, days):
        # Return a small uptrend across all horizons
        return sc._PriceWindow(
            latest_date=date(2026, 5, 7), latest_close=24000.0,
            prior_date=date(2026, 5, 6), prior_close=23900.0,
        )

    async def fake_breadth(days):
        # 60% F&O underlyings up, with comfortably more than min instruments
        return 200, 120

    with (
        patch("src.collectors.sentiment_collector._fetch_vix_component",
              new=AsyncMock(return_value=vix_comp)),
        patch("src.collectors.sentiment_collector._resolve_index_id",
              new=AsyncMock(return_value="fake-index-id")),
        patch("src.collectors.sentiment_collector._index_price_window",
              new=AsyncMock(side_effect=fake_index_window)),
        patch("src.collectors.sentiment_collector._breadth_for_horizon",
              new=AsyncMock(side_effect=fake_breadth)),
    ):
        payload = await sc.compute_sentiment(
            as_of=datetime(2026, 5, 8, 3, 30, tzinfo=timezone.utc),
        )

    assert payload["degraded"] is False
    assert payload["score"] is not None
    assert 4.0 < payload["score"] < 8.0  # sanity range
    # Components all present
    assert payload["components"]["vix"]["score"] == 5.5
    for horizon in ("trend_1d", "trend_1w", "trend_1m"):
        assert payload["components"][horizon]["score"] is not None
    # Weights sum to ~1.0
    assert abs(sum(payload["weights_applied"].values()) - 1.0) < 0.01


@pytest.mark.asyncio
async def test_compute_sentiment_all_degraded_returns_neutral() -> None:
    """Every component fails → score=5.0, degraded=True."""
    from src.collectors import sentiment_collector as sc

    async def fake_index_window(_id, days):
        return sc._PriceWindow(None, None, None, None)

    async def fake_breadth(days):
        return 0, 0  # below min_breadth_instruments

    vix_failed = {"score": None, "reason": "vix unavailable"}

    with (
        patch("src.collectors.sentiment_collector._fetch_vix_component",
              new=AsyncMock(return_value=vix_failed)),
        patch("src.collectors.sentiment_collector._resolve_index_id",
              new=AsyncMock(return_value=None)),
        patch("src.collectors.sentiment_collector._index_price_window",
              new=AsyncMock(side_effect=fake_index_window)),
        patch("src.collectors.sentiment_collector._breadth_for_horizon",
              new=AsyncMock(side_effect=fake_breadth)),
    ):
        payload = await sc.compute_sentiment()

    assert payload["degraded"] is True
    assert payload["score"] == 5.0
    # Each component should have a reason field surfacing the failure
    assert payload["components"]["vix"]["score"] is None
    for horizon in ("trend_1d", "trend_1w", "trend_1m"):
        assert payload["components"][horizon]["score"] is None
        assert "reason" in payload["components"][horizon]


@pytest.mark.asyncio
async def test_compute_sentiment_partial_degradation_renormalises() -> None:
    """If only 1m horizon's breadth + index legs fail, the weight redistributes."""
    from src.collectors import sentiment_collector as sc

    vix_comp = {"value": 16.0, "regime": "neutral", "score": 5.5}

    async def fake_index_window(_id, days):
        if days == 21:  # 1m horizon — pretend we don't have enough history
            return sc._PriceWindow(date(2026, 5, 7), 24000.0, None, None)
        return sc._PriceWindow(date(2026, 5, 7), 24000.0,
                               date(2026, 5, 7) - timedelta(days=days), 23900.0)

    async def fake_breadth(days):
        if days == 21:
            return 10, 5  # below min_breadth_instruments → breadth leg None
        return 200, 120

    with (
        patch("src.collectors.sentiment_collector._fetch_vix_component",
              new=AsyncMock(return_value=vix_comp)),
        patch("src.collectors.sentiment_collector._resolve_index_id",
              new=AsyncMock(return_value="fake-index-id")),
        patch("src.collectors.sentiment_collector._index_price_window",
              new=AsyncMock(side_effect=fake_index_window)),
        patch("src.collectors.sentiment_collector._breadth_for_horizon",
              new=AsyncMock(side_effect=fake_breadth)),
    ):
        payload = await sc.compute_sentiment()

    assert payload["degraded"] is False
    assert payload["components"]["trend_1m"]["score"] is None
    assert "reason" in payload["components"]["trend_1m"]
    # 1m weight should be redistributed — applied weights don't include it
    assert "trend_1m" not in payload["weights_applied"]
    assert abs(sum(payload["weights_applied"].values()) - 1.0) < 0.01
