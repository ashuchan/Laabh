"""Tests for VIX regime classification."""
from __future__ import annotations

import pytest

from src.fno.vix_collector import classify_regime


@pytest.mark.parametrize(
    "vix,expected",
    [
        (11.9, "low"),
        (12.0, "neutral"),  # boundary: 12 is NOT low (< 12 is low)
        (15.0, "neutral"),
        (18.0, "neutral"),  # boundary: 18 is NOT high (> 18 is high)
        (18.1, "high"),
        (25.0, "high"),
    ],
)
def test_classify_regime(vix: float, expected: str) -> None:
    assert classify_regime(vix) == expected
