"""Tests for macro collector utilities."""
from __future__ import annotations

import pytest

from src.collectors.macro_collector import (
    SECTOR_MACRO_MAP,
    get_macro_direction,
    get_macro_drivers,
)


@pytest.mark.parametrize(
    "change_pct,expected",
    [
        (2.0, "bullish"),
        (-1.5, "bearish"),
        (0.1, "neutral"),
        (-0.2, "neutral"),
    ],
)
def test_get_macro_direction(change_pct: float, expected: str) -> None:
    assert get_macro_direction("BRENT", change_pct) == expected


def test_get_macro_drivers_known_sector() -> None:
    drivers = get_macro_drivers("Energy")
    assert "BRENT" in drivers


def test_get_macro_drivers_it_sector() -> None:
    drivers = get_macro_drivers("IT")
    assert "NASDAQ_FUTURES" in drivers
    assert "DXY" in drivers


def test_get_macro_drivers_unknown_sector_returns_default() -> None:
    drivers = get_macro_drivers("ZZZ Unknown")
    assert drivers == SECTOR_MACRO_MAP["Default"]


def test_get_macro_drivers_none_sector() -> None:
    drivers = get_macro_drivers(None)
    assert drivers == SECTOR_MACRO_MAP["Default"]


def test_every_sector_has_at_least_one_driver() -> None:
    for sector, drivers in SECTOR_MACRO_MAP.items():
        assert len(drivers) >= 1, f"Sector {sector!r} has no macro drivers"
