"""Tests for F&O catalyst scorer Phase 2 — pure scoring helpers."""
from __future__ import annotations

import pytest

from src.fno.catalyst_scorer import (
    compute_composite,
    score_convergence,
    score_fii_dii,
    score_fii_dii_for_instrument,
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
    # 4 of 4 above 5.5 → +5.0 → 10.0 (cap)
    assert score_convergence(8.0, 8.0, 8.0, 8.0) == 10.0


def test_score_convergence_all_bearish() -> None:
    # 4 of 4 below 4.5 → -5.0 → 0.0 (cap)
    assert score_convergence(2.0, 2.0, 2.0, 2.0) == 0.0


def test_score_convergence_mixed_is_neutral() -> None:
    # 2 bullish + 2 bearish cancel → 5.0
    assert score_convergence(8.0, 2.0, 8.0, 2.0) == 5.0


def test_score_convergence_two_of_four_bullish_lifts_above_neutral() -> None:
    """The behaviour the smoother gradient unlocks — 2 dimensions agreeing
    moves convergence off 5.0 (was locked there in the v1 step-function).
    Each bullish dim adds 5/n = 1.25; 2 bullish → 5.0 + 2.5 = 7.5."""
    # news=10, sentiment=7 (both > 5.5), fii_dii=5 + macro=5 (defaults, neither
    # bullish nor bearish) → 2 bullish, 0 bearish → 7.5
    result = score_convergence(10.0, 7.0, 5.0, 5.0)
    assert result == 7.5


def test_score_convergence_one_of_four_bullish_still_lifts() -> None:
    # 1 bullish, 0 bearish → +1.25 → 6.25
    result = score_convergence(7.0, 5.0, 5.0, 5.0)
    assert result == 6.25


def test_score_convergence_three_of_four_bullish() -> None:
    # 3 bullish, 0 bearish → +3.75 → 8.75
    result = score_convergence(8.0, 7.0, 6.0, 5.0)
    assert result == 8.75


def test_score_convergence_threshold_exact_boundary() -> None:
    # Exactly at the bullish_threshold (5.5) does NOT count — must be > 5.5
    result = score_convergence(5.5, 5.5, 5.5, 5.5)
    assert result == 5.0


def test_score_convergence_thresholds_overridable_for_calibration() -> None:
    # Operator wants to require stronger signal — bullish threshold = 7.0
    # 8.0 still bullish, 6.5 no longer counts
    result = score_convergence(
        8.0, 6.5, 6.5, 6.5,
        bullish_threshold=7.0, bearish_threshold=3.0,
    )
    # Only 1 bullish at the higher threshold → 5 + 1.25 = 6.25
    assert result == 6.25


# ---------------------------------------------------------------------------
# score_fii_dii_for_instrument — per-stock alignment proxy
# ---------------------------------------------------------------------------

def test_fii_dii_for_instrument_no_pct_change_falls_back_to_market() -> None:
    """Missing price data → just return the unmodulated market-wide score."""
    result = score_fii_dii_for_instrument(600.0, 400.0, None)
    assert result == score_fii_dii(600.0, 400.0)  # 10.0


def test_fii_dii_for_instrument_alignment_bullish_market_up_stock() -> None:
    """Market FII bullish + stock up → +1.5 alignment bonus."""
    market_score = score_fii_dii(600.0, 400.0)  # 10.0
    result = score_fii_dii_for_instrument(600.0, 400.0, stock_pct_change=2.0)
    # 10.0 + 1.5 capped at 10.0
    assert result == 10.0
    # Verify with a non-saturated case
    market_score2 = score_fii_dii(200.0, 100.0)  # 2 + 1.67 / 2 = ~3.83 hmm
    # Actually let me trace: fii=200/500=0.4 → 5+0.4*5=7.0; dii=100/300=0.33 → 5+0.33*5=6.67
    # avg = 6.83. > 5.5 bullish. + 1.5 → 8.33
    result2 = score_fii_dii_for_instrument(200.0, 100.0, stock_pct_change=1.5)
    assert result2 > market_score2


def test_fii_dii_for_instrument_divergence_bullish_market_down_stock() -> None:
    """Market FII bullish + stock down → -1.5 divergence penalty."""
    base = score_fii_dii(200.0, 100.0)  # bullish, >5.5
    result = score_fii_dii_for_instrument(200.0, 100.0, stock_pct_change=-2.0)
    assert result < base
    assert result == round(base - 1.5, 2)


def test_fii_dii_for_instrument_alignment_bearish_market_down_stock() -> None:
    """Market FII bearish + stock down → +1.5 (alignment with selling)."""
    base = score_fii_dii(-300.0, -200.0)  # bearish, <4.5
    result = score_fii_dii_for_instrument(-300.0, -200.0, stock_pct_change=-2.0)
    assert result > base
    assert result == round(base + 1.5, 2)


def test_fii_dii_for_instrument_neutral_market_no_modulation() -> None:
    """Market in 4.5-5.5 band → return raw market score regardless of stock."""
    # FII flat, DII flat → neutral 5.0
    result_up = score_fii_dii_for_instrument(0.0, 0.0, stock_pct_change=2.0)
    result_dn = score_fii_dii_for_instrument(0.0, 0.0, stock_pct_change=-2.0)
    assert result_up == 5.0
    assert result_dn == 5.0


def test_fii_dii_for_instrument_small_stock_move_no_modulation() -> None:
    """Stock pct < ±0.5% → no clear directional read → no bonus/penalty."""
    base = score_fii_dii(600.0, 400.0)
    result = score_fii_dii_for_instrument(600.0, 400.0, stock_pct_change=0.2)
    assert result == base


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
