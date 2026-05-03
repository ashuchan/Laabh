"""Tests for Task 4 — bhavcopy fetcher.

Uses respx to mock the NSE archive HTTP responses.
"""
from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
import respx
import httpx

from src.dryrun.bhavcopy import (
    BhavcopyMissingError,
    _extract_csv_from_zip,
    _parse_fo_csv,
    fetch_fo_bhavcopy,
)

_SAMPLE_FO_CSV = """\
FINSymbol,Instrument,Expiry_Dt,StrikePrice,OptionType,OpenPric,HighPric,LowPric,ClosingPric,SetlPric,No_of_Contracts,Val_inLakh,Open_Int,Chng_in_Opn_Int,LastPric
NIFTY,OPTIDX,2026-04-24,22000,CE,150.0,160.0,145.0,155.0,155.0,1000,500000,50000,200,155.0
NIFTY,OPTIDX,2026-04-24,22000,PE,130.0,135.0,125.0,132.0,132.0,800,400000,45000,100,132.0
"""


def _make_zip(csv_content: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("BhavCopy_NSE_FO_test.csv", csv_content)
    return buf.getvalue()


def test_extract_csv_from_zip():
    data = _make_zip(_SAMPLE_FO_CSV)
    csv = _extract_csv_from_zip(data)
    assert "NIFTY" in csv
    assert "OPTIDX" in csv


def test_parse_fo_csv_columns():
    df = _parse_fo_csv(_SAMPLE_FO_CSV)
    assert "symbol" in df.columns
    assert "option_type" in df.columns
    assert "oi" in df.columns
    assert "strike_price" in df.columns
    # Should have CE and PE rows
    assert set(df["option_type"].unique()) == {"CE", "PE"}


def test_parse_fo_csv_numeric_coercion():
    df = _parse_fo_csv(_SAMPLE_FO_CSV)
    assert df["oi"].dtype.kind in ("f", "i", "u")  # numeric
    assert df["strike_price"].dtype.kind in ("f", "i", "u")  # numeric


@pytest.mark.asyncio
@respx.mock
async def test_fetch_fo_bhavcopy_happy_path(tmp_path):
    """Happy path: download, parse, cache."""
    d = date(2026, 4, 23)
    zip_data = _make_zip(_SAMPLE_FO_CSV)
    url_pattern = respx.get(url__regex=r"nsearchives\.nseindia\.com")
    url_pattern.mock(return_value=httpx.Response(200, content=zip_data))
    # Also mock the NSE homepage cookie request
    respx.get("https://www.nseindia.com").mock(return_value=httpx.Response(200))

    with patch("src.dryrun.bhavcopy._SETTINGS") as mock_settings:
        mock_settings.dryrun_bhavcopy_cache_dir = str(tmp_path)
        df = await fetch_fo_bhavcopy(d)

    assert len(df) > 0
    assert "symbol" in df.columns


@pytest.mark.asyncio
@respx.mock
async def test_fetch_fo_bhavcopy_404_raises(tmp_path):
    """404 from NSE archive raises BhavcopyMissingError."""
    d = date(2026, 4, 23)
    respx.get("https://www.nseindia.com").mock(return_value=httpx.Response(200))
    respx.get(url__regex=r"nsearchives\.nseindia\.com").mock(
        return_value=httpx.Response(404)
    )

    with (
        patch("src.dryrun.bhavcopy._SETTINGS") as mock_settings,
        pytest.raises(BhavcopyMissingError),
    ):
        mock_settings.dryrun_bhavcopy_cache_dir = str(tmp_path)
        await fetch_fo_bhavcopy(d)


@pytest.mark.asyncio
async def test_fetch_fo_bhavcopy_cache_hit(tmp_path):
    """Second call uses cache and makes no HTTP request."""
    import pandas as pd
    d = date(2026, 4, 23)
    df = _parse_fo_csv(_SAMPLE_FO_CSV)
    cache_path = tmp_path / f"fo_{d.strftime('%Y%m%d')}.parquet"
    df.to_parquet(cache_path, index=False)

    with (
        patch("src.dryrun.bhavcopy._SETTINGS") as mock_settings,
        patch("src.dryrun.bhavcopy._download_zip") as mock_dl,
    ):
        mock_settings.dryrun_bhavcopy_cache_dir = str(tmp_path)
        result = await fetch_fo_bhavcopy(d)

    assert not mock_dl.called
    assert len(result) == len(df)
