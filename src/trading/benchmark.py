"""Benchmark comparison — Nifty 50 / Sensex return vs portfolio return."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import yfinance as yf
from loguru import logger
from sqlalchemy import select

from src.db import session_scope
from src.models.portfolio import Portfolio, PortfolioSnapshot


# Yahoo Finance tickers for Indian indices
BENCHMARK_TICKERS = {
    "NIFTY 50": "^NSEI",
    "SENSEX": "^BSESN",
}


class BenchmarkTracker:
    """Fetches benchmark index data and compares against portfolio snapshots."""

    async def get_benchmark_return_pct(
        self, symbol: str, since: date
    ) -> Decimal | None:
        """Return the % change in the benchmark index from `since` to today."""
        ticker_sym = BENCHMARK_TICKERS.get(symbol)
        if not ticker_sym:
            logger.warning(f"unknown benchmark symbol: {symbol}")
            return None
        try:
            df = yf.download(ticker_sym, start=str(since), auto_adjust=True, progress=False)
            if df.empty:
                return None
            start_price = Decimal(str(df["Close"].iloc[0]))
            end_price = Decimal(str(df["Close"].iloc[-1]))
            return (end_price - start_price) / start_price * 100
        except Exception as exc:
            logger.error(f"benchmark fetch failed for {symbol}: {exc}")
            return None

    async def update_portfolio_benchmarks(self) -> None:
        """Update benchmark_start on portfolios that don't have it yet."""
        async with session_scope() as session:
            result = await session.execute(
                select(Portfolio).where(
                    Portfolio.is_active == True,
                    Portfolio.benchmark_start == None,
                )
            )
            portfolios = result.scalars().all()

        for portfolio in portfolios:
            ticker_sym = BENCHMARK_TICKERS.get(portfolio.benchmark_symbol, "^NSEI")
            try:
                df = yf.download(
                    ticker_sym,
                    start=str(portfolio.created_at.date()),
                    end=str(portfolio.created_at.date()),
                    progress=False,
                )
                if not df.empty:
                    start_val = float(df["Close"].iloc[0])
                    async with session_scope() as session:
                        p = await session.get(Portfolio, portfolio.id)
                        if p:
                            p.benchmark_start = start_val
            except Exception as exc:
                logger.error(f"benchmark init failed: {exc}")

    async def enrich_snapshots_with_benchmark(self, portfolio_id: str) -> None:
        """Back-fill benchmark_value and benchmark_pnl_pct on snapshots missing it."""
        async with session_scope() as session:
            portfolio = await session.get(Portfolio, portfolio_id)
            if not portfolio or not portfolio.benchmark_start:
                return

            result = await session.execute(
                select(PortfolioSnapshot)
                .where(
                    PortfolioSnapshot.portfolio_id == portfolio_id,
                    PortfolioSnapshot.benchmark_value == None,
                )
                .order_by(PortfolioSnapshot.date)
            )
            snaps = result.scalars().all()

        if not snaps:
            return

        ticker_sym = BENCHMARK_TICKERS.get(
            portfolio.benchmark_symbol or "NIFTY 50", "^NSEI"
        )
        dates = [s.date for s in snaps]
        try:
            df = yf.download(
                ticker_sym,
                start=str(min(dates)),
                end=str(max(dates)),
                progress=False,
            )
            for snap in snaps:
                row = df["Close"].get(str(snap.date))
                if row is not None:
                    benchmark_val = float(row)
                    start = portfolio.benchmark_start
                    pct = (benchmark_val - start) / start * 100 if start else 0.0
                    async with session_scope() as session:
                        s = await session.execute(
                            select(PortfolioSnapshot).where(
                                PortfolioSnapshot.portfolio_id == portfolio_id,
                                PortfolioSnapshot.date == snap.date,
                            )
                        )
                        srow = s.scalar_one_or_none()
                        if srow:
                            srow.benchmark_value = benchmark_val
                            srow.benchmark_pnl_pct = pct
        except Exception as exc:
            logger.error(f"benchmark enrichment failed: {exc}")
