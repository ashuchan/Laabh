"""Tests for entity matching aliases."""
from __future__ import annotations

from src.extraction.entity_matcher import ALIASES


def test_common_aliases_present() -> None:
    assert ALIASES["RIL"] == "RELIANCE"
    assert ALIASES["SBI"] == "SBIN"
    assert ALIASES["INFOSYS"] == "INFY"
    assert ALIASES["HUL"] == "HINDUNILVR"
