"""Tests for F&O catalyst scorer Phase 2 — pure scoring helpers."""
from __future__ import annotations

import pytest

from src.fno.catalyst_scorer import (
    compute_composite,
    score_convergence,
    score_fii_dii,
    score_macro,
    score_news,
)


# ---------------------------------------------------------------------------
# score_news
# ---------------------------------------------------------------------------

def test_score_news_all_bullish() -> None:
    assert score_news(5, 0) == 10.0


def test_score_news_all_bearish() -> None:
    assert score_news(0, 5) == 0.0


def test_score_news_neutral() -> None:
    assert score_news(0, 0) == 5.0


def test_score_news_mixed() -> None:
    # 3 bullish, 1 bearish → net=2, total=4, ratio=0.5 → 5 + 2.5 = 7.5
    result = score_news(3, 1)
    assert abs(result - 7.5) < 0.01


def test_score_news_equal() -> None:
    assert score_news(3, 3) == 5.0


# ---------------------------------------------------------------------------
# score_fii_dii
# ---------------------------------------------------------------------------

def test_score_fii_dii_both_buying() -> None:
    result = score_fii_dii(600.0, 400.0)
    assert result == 10.0


def test_score_fii_dii_both_selling() -> None:
    result = score_fii_dii(-600.0, -400.0)
    assert result == 0.0


def test_score_fii_dii_neutral() -> None:
    result = score_fii_dii(0.0, 0.0)
    assert result == 5.0


def test_score_fii_dii_mixed() -> None:
    result = score_fii_dii(600.0, -400.0)
    # fii=10, dii=0 → avg=5
    assert result == 5.0


# ---------------------------------------------------------------------------
# score_macro
# ---------------------------------------------------------------------------

def test_score_macro_all_bullish() -> None:
    # Energy sector: BRENT, WTI both bullish (>0.3%)
    result = score_macro("Energy", {"BRENT": 1.5, "WTI": 2.0})
    assert result == 10.0


def test_score_macro_all_bearish() -> None:
    result = score_macro("Energy", {"BRENT": -2.0, "WTI": -1.5})
    assert result == 0.0


def test_score_macro_neutral_change() -> None:
    result = score_macro("Energy", {"BRENT": 0.1, "WTI": -0.1})
    assert result == 5.0


def test_score_macro_missing_data_neutral() -> None:
    result = score_macro("Energy", {})
    assert result == 5.0


def test_score_macro_none_sector_default() -> None:
    result = score_macro(None, {"SPX_FUTURES": 1.0})
    assert result == 10.0


# ---------------------------------------------------------------------------
# score_convergence
# ---------------------------------------------------------------------------

def test_score_convergence_all_bullish() -> None:
    result = score_convergence(8.0, 8.0, 8.0, 8.0)
    assert result > 5.0


def test_score_convergence_all_bearish() -> None:
    result = score_convergence(2.0, 2.0, 2.0, 2.0)
    assert result < 5.0


def test_score_convergence_mixed_is_neutral() -> None:
    result = score_convergence(8.0, 2.0, 8.0, 2.0)
    assert result == 5.0


# ---------------------------------------------------------------------------
# compute_composite
# ---------------------------------------------------------------------------

def test_compute_composite_all_max() -> None:
    result = compute_composite(10.0, 10.0, 10.0, 10.0, 10.0)
    assert result == 10.0


def test_compute_composite_all_zero() -> None:
    result = compute_composite(0.0, 0.0, 0.0, 0.0, 0.0)
    assert result == 0.0


def test_compute_composite_all_neutral() -> None:
    result = compute_composite(5.0, 5.0, 5.0, 5.0, 5.0)
    assert result == 5.0


def test_compute_composite_convergence_weighted_higher() -> None:
    # convergence=10, others=5 → convergence's w=1.5 pulls composite above 5
    result = compute_composite(5.0, 5.0, 5.0, 5.0, 10.0, w_convergence=1.5)
    assert result > 5.0


def test_compute_composite_custom_weights() -> None:
    # All equal weight → simple average
    result = compute_composite(
        8.0, 6.0, 4.0, 2.0, 5.0,
        w_news=1.0, w_sentiment=1.0, w_fii_dii=1.0, w_macro=1.0, w_convergence=1.0,
    )
    assert abs(result - 5.0) < 0.01  # (8+6+4+2+5)/5 = 5.0
