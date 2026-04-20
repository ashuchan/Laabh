"""Tests for signal resolution and analyst scoring."""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.analytics.analyst_tracker import AnalystTracker


def test_analyst_tracker_weights_sum_to_one():
    tracker = AnalystTracker()
    total = (
        tracker.HIT_RATE_WEIGHT
        + tracker.RETURN_WEIGHT
        + tracker.CONSISTENCY_WEIGHT
        + tracker.RECENCY_WEIGHT
    )
    assert total == Decimal("1.00")


def test_analyst_tracker_return_cap():
    tracker = AnalystTracker()
    assert tracker.RETURN_CAP == Decimal("10")


def test_analyst_tracker_min_signals():
    tracker = AnalystTracker()
    assert tracker.MIN_SIGNALS == 5


def test_convergence_threshold():
    from src.analytics.convergence import ConvergenceEngine
    engine = ConvergenceEngine()
    assert engine.HIGH_PRIORITY_THRESHOLD == 4
    assert engine.CRITICAL_THRESHOLD == 5
    assert engine.WINDOW_HOURS == 24


def test_risk_manager_position_limit():
    from src.trading.risk_manager import RiskManager
    rm = RiskManager()
    assert rm.MAX_POSITION_PCT == Decimal("0.10")
