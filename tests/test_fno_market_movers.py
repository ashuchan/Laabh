"""Tests for src.fno.market_movers — pure logic + bhavcopy I/O via mocks."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from src.fno.market_movers import (
    MarketMovers,
    Mover,
    _resolve_target_date,
    get_top_fno_movers,
    render_movers_block,
)


# ---------------------------------------------------------------------------
# _resolve_target_date — pure
# ---------------------------------------------------------------------------

def test_resolve_target_date_uses_yesterday_in_ist() -> None:
    # 2026-05-08 09:00 IST → target = 2026-05-07
    ist_morning = datetime(2026, 5, 8, 3, 30, tzinfo=timezone.utc)  # = 09:00 IST
    assert _resolve_target_date(ist_morning) == date(2026, 5, 7)


def test_resolve_target_date_handles_late_night_utc() -> None:
    # 2026-05-07 19:00 UTC = 2026-05-08 00:30 IST → target = 2026-05-07
    late_utc = datetime(2026, 5, 7, 19, 0, tzinfo=timezone.utc)
    assert _resolve_target_date(late_utc) == date(2026, 5, 7)


def test_resolve_target_date_default_is_now_minus_one_ist() -> None:
    # Just confirm it returns a date, not None — the value depends on wall clock
    assert isinstance(_resolve_target_date(None), date)


# ---------------------------------------------------------------------------
# Fixtures: tiny synthetic bhavcopy DataFrames
# ---------------------------------------------------------------------------

def _fake_fo_df(symbols: list[str]) -> pd.DataFrame:
    """F&O bhavcopy with one option row per symbol — gives the universe set."""
    return pd.DataFrame({
        "symbol": symbols,
        "instrument": ["OPTSTK"] * len(symbols),
        "option_type": ["CE"] * len(symbols),
        "strike_price": [100.0] * len(symbols),
        "expiry_date": [date(2026, 5, 29)] * len(symbols),
    })


def _fake_cm_df(rows: list[tuple[str, float, float, str]]) -> pd.DataFrame:
    """Cash-market bhavcopy. rows = (symbol, prev_close, close, series)."""
    return pd.DataFrame({
        "symbol": [r[0] for r in rows],
        "prev_close": [r[1] for r in rows],
        "close": [r[2] for r in rows],
        "series": [r[3] for r in rows],
        "instrument_type": ["STK"] * len(rows),
        "open": [r[1] for r in rows],
        "high": [max(r[1], r[2]) for r in rows],
        "low": [min(r[1], r[2]) for r in rows],
    })


# ---------------------------------------------------------------------------
# get_top_fno_movers — happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_top_fno_movers_ranks_by_pct_change() -> None:
    fo_df = _fake_fo_df(["AAA", "BBB", "CCC", "DDD", "EEE"])
    cm_df = _fake_cm_df([
        ("AAA", 100.0, 110.0, "EQ"),  # +10%
        ("BBB", 200.0, 210.0, "EQ"),  # +5%
        ("CCC", 50.0, 49.0, "EQ"),    # -2%
        ("DDD", 300.0, 270.0, "EQ"),  # -10%
        ("EEE", 400.0, 404.0, "EQ"),  # +1%
    ])
    with patch(
        "src.fno.market_movers.fetch_fo_bhavcopy",
        new=AsyncMock(return_value=fo_df),
    ), patch(
        "src.fno.market_movers.fetch_cm_bhavcopy",
        new=AsyncMock(return_value=cm_df),
    ):
        result = await get_top_fno_movers(top_n=2, bottom_n=2)

    assert isinstance(result, MarketMovers)
    assert [m.symbol for m in result.top] == ["AAA", "BBB"]
    assert [m.symbol for m in result.bottom] == ["DDD", "CCC"]
    assert result.top[0].pct_change == pytest.approx(10.0)
    assert result.bottom[0].pct_change == pytest.approx(-10.0)
    # Ranks are 1-indexed within each list
    assert result.top[0].rank == 1
    assert result.top[1].rank == 2
    assert result.bottom[0].rank == 1


@pytest.mark.asyncio
async def test_get_top_fno_movers_filters_non_eq_series() -> None:
    fo_df = _fake_fo_df(["AAA", "BBB"])
    cm_df = _fake_cm_df([
        ("AAA", 100.0, 200.0, "BE"),   # +100% but BE series → excluded
        ("BBB", 100.0, 110.0, "EQ"),   # +10%
    ])
    with patch(
        "src.fno.market_movers.fetch_fo_bhavcopy",
        new=AsyncMock(return_value=fo_df),
    ), patch(
        "src.fno.market_movers.fetch_cm_bhavcopy",
        new=AsyncMock(return_value=cm_df),
    ):
        result = await get_top_fno_movers(top_n=5, bottom_n=5)
    assert [m.symbol for m in result.top] == ["BBB"]


@pytest.mark.asyncio
async def test_get_top_fno_movers_filters_non_fno_universe() -> None:
    """A CM symbol not present in the F&O bhavcopy must be ignored."""
    fo_df = _fake_fo_df(["AAA"])
    cm_df = _fake_cm_df([
        ("AAA", 100.0, 105.0, "EQ"),
        ("ZZZ", 100.0, 200.0, "EQ"),  # +100% but not F&O-listed
    ])
    with patch(
        "src.fno.market_movers.fetch_fo_bhavcopy",
        new=AsyncMock(return_value=fo_df),
    ), patch(
        "src.fno.market_movers.fetch_cm_bhavcopy",
        new=AsyncMock(return_value=cm_df),
    ):
        result = await get_top_fno_movers(top_n=5, bottom_n=5)
    symbols = {m.symbol for m in result.top}
    assert symbols == {"AAA"}


@pytest.mark.asyncio
async def test_get_top_fno_movers_drops_zero_prev_close() -> None:
    fo_df = _fake_fo_df(["AAA", "BBB"])
    cm_df = _fake_cm_df([
        ("AAA", 0.0, 50.0, "EQ"),       # divide-by-zero would explode
        ("BBB", 100.0, 110.0, "EQ"),
    ])
    with patch(
        "src.fno.market_movers.fetch_fo_bhavcopy",
        new=AsyncMock(return_value=fo_df),
    ), patch(
        "src.fno.market_movers.fetch_cm_bhavcopy",
        new=AsyncMock(return_value=cm_df),
    ):
        result = await get_top_fno_movers(top_n=5, bottom_n=5)
    assert [m.symbol for m in result.top] == ["BBB"]


@pytest.mark.asyncio
async def test_get_top_fno_movers_walks_back_through_holidays() -> None:
    """If the first date 404s, we should try the day before."""
    from src.dryrun.bhavcopy import BhavcopyMissingError

    fo_df = _fake_fo_df(["AAA"])
    cm_df = _fake_cm_df([("AAA", 100.0, 105.0, "EQ")])

    fo_calls = AsyncMock(side_effect=[
        BhavcopyMissingError("404"),
        fo_df,
    ])
    cm_calls = AsyncMock(return_value=cm_df)

    as_of = datetime(2026, 5, 11, 9, 0, tzinfo=timezone.utc)  # Mon
    with patch(
        "src.fno.market_movers.fetch_fo_bhavcopy", new=fo_calls,
    ), patch(
        "src.fno.market_movers.fetch_cm_bhavcopy", new=cm_calls,
    ):
        result = await get_top_fno_movers(top_n=5, bottom_n=5, as_of=as_of)

    assert fo_calls.await_count == 2
    # Second call walked back one day from initial target
    assert result.top and result.top[0].symbol == "AAA"


@pytest.mark.asyncio
async def test_get_top_fno_movers_returns_empty_when_archive_unavailable() -> None:
    from src.dryrun.bhavcopy import BhavcopyMissingError

    with patch(
        "src.fno.market_movers.fetch_fo_bhavcopy",
        new=AsyncMock(side_effect=BhavcopyMissingError("404")),
    ), patch(
        "src.fno.market_movers.fetch_cm_bhavcopy",
        new=AsyncMock(side_effect=BhavcopyMissingError("404")),
    ):
        result = await get_top_fno_movers(top_n=5, bottom_n=5)
    assert result.top == []
    assert result.bottom == []


@pytest.mark.asyncio
async def test_get_top_fno_movers_preserves_decimal_precision() -> None:
    """Decimal close/prev_close should be exact (no float round-trip loss)."""
    fo_df = _fake_fo_df(["AAA"])
    cm_df = pd.DataFrame({
        "symbol": ["AAA"],
        "prev_close": [100.10],
        "close": [105.55],
        "series": ["EQ"],
        "instrument_type": ["STK"],
    })
    with patch(
        "src.fno.market_movers.fetch_fo_bhavcopy",
        new=AsyncMock(return_value=fo_df),
    ), patch(
        "src.fno.market_movers.fetch_cm_bhavcopy",
        new=AsyncMock(return_value=cm_df),
    ):
        result = await get_top_fno_movers(top_n=1, bottom_n=0)
    m = result.top[0]
    # Exact match — would fail under float() round-trip if pandas stored
    # 105.55 as 105.54999999999998… and we coerced through float first.
    assert str(m.close) == "105.55"
    assert str(m.prev_close) == "100.1"


# ---------------------------------------------------------------------------
# render_movers_block — pure
# ---------------------------------------------------------------------------

def _sample_movers() -> MarketMovers:
    return MarketMovers(
        as_of_date=date(2026, 5, 7),
        top=[
            Mover("PAYTM", Decimal("1110.60"), Decimal("1197.40"),
                  Decimal("86.80"), 7.82, 1),
            Mover("BHARATFORG", Decimal("1873.80"), Decimal("1992.90"),
                  Decimal("119.10"), 6.36, 2),
        ],
        bottom=[
            Mover("GODREJCP", Decimal("1094.10"), Decimal("1036.60"),
                  Decimal("-57.50"), -5.26, 1),
        ],
    )


def test_render_movers_block_includes_leaders_and_laggards() -> None:
    block = render_movers_block(_sample_movers())
    assert "F&O LEADERS" in block
    assert "F&O LAGGARDS" in block
    assert "PAYTM" in block
    assert "BHARATFORG" in block
    assert "GODREJCP" in block
    assert "+7.82%" in block
    assert "-5.26%" in block


def test_render_movers_block_annotates_matching_symbol() -> None:
    block = render_movers_block(_sample_movers(), instrument_symbol="BHARATFORG")
    assert "THIS INSTRUMENT YESTERDAY" in block
    assert "rank #2 gainer" in block


def test_render_movers_block_annotates_loser_match() -> None:
    block = render_movers_block(_sample_movers(), instrument_symbol="godrejcp")
    assert "rank #1 loser" in block


def test_render_movers_block_no_annotation_for_unmatched_symbol() -> None:
    block = render_movers_block(_sample_movers(), instrument_symbol="RELIANCE")
    assert "THIS INSTRUMENT YESTERDAY" not in block


def test_render_movers_block_empty_falls_back_gracefully() -> None:
    empty = MarketMovers(as_of_date=date(2026, 5, 7), top=[], bottom=[])
    block = render_movers_block(empty)
    assert "no prior-session bhavcopy" in block
