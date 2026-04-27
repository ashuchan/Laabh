"""Tests for F&O ban list parsing and URL formatting."""
from __future__ import annotations

from datetime import date

import pytest

from src.fno.ban_list import _format_date, _parse_symbols


def test_format_date() -> None:
    assert _format_date(date(2026, 4, 27)) == "27042026"
    assert _format_date(date(2026, 1, 5)) == "05012026"


def test_parse_symbols_normal_csv() -> None:
    csv_text = "IRCTC\nMANAPPURAM\nRBLBANK\n"
    symbols = _parse_symbols(csv_text)
    assert symbols == ["IRCTC", "MANAPPURAM", "RBLBANK"]


def test_parse_symbols_with_header() -> None:
    csv_text = "SECURITY\nIRCTC\nMANAPPURAM\n"
    symbols = _parse_symbols(csv_text)
    # "SECURITY" is a header and should be filtered out
    assert "SECURITY" not in symbols
    assert "IRCTC" in symbols


def test_parse_symbols_empty_csv() -> None:
    assert _parse_symbols("") == []
    assert _parse_symbols("\n\n") == []


def test_parse_symbols_lowercased_input() -> None:
    csv_text = "irctc\nManappuram\n"
    symbols = _parse_symbols(csv_text)
    assert symbols == ["IRCTC", "MANAPPURAM"]


def test_parse_symbols_extra_whitespace() -> None:
    csv_text = "  IRCTC  \n  RBLBANK  \n"
    symbols = _parse_symbols(csv_text)
    assert "IRCTC" in symbols
    assert "RBLBANK" in symbols
