"""Tests for FII/DII data parsing."""
from __future__ import annotations

import pytest

from src.collectors.fii_dii_collector import _parse_fii_dii


def test_parse_fii_dii_basic() -> None:
    records = [
        {"category": "FII/FPI", "buyValue": "1000.5", "sellValue": "800.2", "date": "27-Apr-2026"},
        {"category": "DII", "buyValue": "500.0", "sellValue": "400.0", "date": "27-Apr-2026"},
    ]
    result = _parse_fii_dii(records)
    assert abs(result["fii_net_cr"] - 200.3) < 0.01
    assert abs(result["dii_net_cr"] - 100.0) < 0.01
    assert result["date"] == "27-Apr-2026"


def test_parse_fii_dii_empty() -> None:
    result = _parse_fii_dii([])
    assert result["fii_net_cr"] == 0.0
    assert result["dii_net_cr"] == 0.0
    assert result["date"] is None


def test_parse_fii_dii_fpi_variant() -> None:
    records = [
        {"category": "FPI", "buyValue": "500", "sellValue": "300", "date": "27-Apr-2026"},
    ]
    result = _parse_fii_dii(records)
    assert result["fii_net_cr"] == 200.0


def test_parse_fii_dii_missing_values() -> None:
    records = [
        {"category": "FII", "buyValue": None, "sellValue": None, "date": "27-Apr-2026"},
    ]
    result = _parse_fii_dii(records)
    assert result["fii_net_cr"] == 0.0
