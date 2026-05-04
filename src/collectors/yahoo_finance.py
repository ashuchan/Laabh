"""Fallback price data via yfinance — for EOD and historical backfill."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

import yfinance as yf
from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.collectors.base import BaseCollector, CollectorResult
from src.db import session_scope
from src.models.instrument import Instrument
from src.models.price import PriceDaily


class YahooFinanceCollector(BaseCollector):
    """Fetch EOD OHLCV for all active instruments from Yahoo Finance."""

    job_name = "yahoo_finance_eod"

    def __init__(self, days: int = 1, symbols: list[str] | None = None) -> None:
        """`days` = how many days back to fetch; `symbols` = optional filter list."""
        super().__init__(source_id=None)
        self.days = days
        self.symbol_filter = symbols

    async def _collect(self) -> CollectorResult:
        result = CollectorResult()
        async with session_scope() as session:
            q = select(Instrument).where(
                Instrument.is_active == True,  # noqa: E712
                Instrument.yahoo_symbol.is_not(None),
            )
            if self.symbol_filter:
                q = q.where(Instrument.symbol.in_(self.symbol_filter))
            rows = await session.execute(q)
            instruments = list(rows.scalars())

        for inst in instruments:
            try:
                count = await self._fetch_one(inst)
                result.items_new += count
                result.items_fetched += count
            except Exception as exc:
                logger.warning(f"yfinance {inst.symbol}: {exc}")
                result.errors.append(f"{inst.symbol}: {exc}")
        return result

    async def _fetch_one(self, inst: Instrument) -> int:
        start = (datetime.utcnow() - timedelta(days=self.days + 2)).date()
        end = (datetime.utcnow() + timedelta(days=1)).date()
        df = await asyncio.to_thread(
            yf.download,
            inst.yahoo_symbol,
            start=start,
            end=end,
            progress=False,
            auto_adjust=False,
        )
        if df is None or df.empty:
            return 0

        # Newer yfinance returns a MultiIndex on columns even for a single
        # ticker (e.g. ("Open", "RELIANCE.NS")) which makes row["Open"] return
        # a Series, breaking float() conversion. Flatten by dropping level 1.
        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df.columns = df.columns.get_level_values(0)

        def _scalar(row, col):
            v = row[col]
            if hasattr(v, "iloc"):  # still a Series — take the first value
                v = v.iloc[0]
            return v

        rows_inserted = 0
        async with session_scope() as session:
            for idx, row in df.iterrows():
                d: date = idx.date() if hasattr(idx, "date") else idx
                op, hi, lo, cl, vol = (
                    _scalar(row, "Open"), _scalar(row, "High"),
                    _scalar(row, "Low"), _scalar(row, "Close"),
                    _scalar(row, "Volume"),
                )
                payload = {
                    "instrument_id": inst.id,
                    "date": d,
                    "open": float(op) if not _isnan(op) else None,
                    "high": float(hi) if not _isnan(hi) else None,
                    "low": float(lo) if not _isnan(lo) else None,
                    "close": float(cl) if not _isnan(cl) else None,
                    "volume": int(vol) if not _isnan(vol) else None,
                }
                stmt = pg_insert(PriceDaily).values(**payload)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["instrument_id", "date"],
                    set_={k: stmt.excluded[k] for k in ("open", "high", "low", "close", "volume")},
                )
                await session.execute(stmt)
                rows_inserted += 1
        return rows_inserted


def _isnan(v: float) -> bool:
    return v is None or (isinstance(v, float) and v != v)
