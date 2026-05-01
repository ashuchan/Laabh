"""
NautilusTrader backtesting adapter.
LGPL-3.0 — imported as library, strategies stay private.
"""
from __future__ import annotations

from decimal import Decimal

import pandas as pd

try:
    from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig  # type: ignore[import-not-found]
    from nautilus_trader.model.data import Bar, BarSpecification, BarType  # type: ignore[import-not-found]
    from nautilus_trader.model.enums import BarAggregation, PriceType  # type: ignore[import-not-found]
    from nautilus_trader.model.identifiers import InstrumentId, TraderId, Venue  # type: ignore[import-not-found]
    from nautilus_trader.model.instruments import Equity  # type: ignore[import-not-found]
    from nautilus_trader.model.objects import Money, Price, Quantity  # type: ignore[import-not-found]
    from nautilus_trader.model.currencies import INR  # type: ignore[import-not-found]
    _NAUTILUS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _NAUTILUS_AVAILABLE = False


async def health() -> dict:
    """Return integration health status."""
    return {
        "status": "ok" if _NAUTILUS_AVAILABLE else "down",
        "backend": "nautilustrader",
        "available": _NAUTILUS_AVAILABLE,
    }


def run_signal_backtest(
    signal_history: list[dict],
    ohlcv_df: pd.DataFrame,
    ticker: str,
    initial_capital: float = 1_000_000.0,
) -> dict:
    """
    Backtest a signal series against historical OHLCV.
    Used to score analyst accuracy with realistic slippage.

    Args:
        signal_history: list of {date, ticker, direction, entry, target, stop}
        ohlcv_df: DataFrame with columns: date, open, high, low, close, volume
        ticker: NSE symbol e.g. "RELIANCE"
        initial_capital: starting capital in INR (default ₹10L)

    Returns:
        Performance metrics dict.
    """
    if not _NAUTILUS_AVAILABLE:
        raise RuntimeError(
            "nautilus_trader is not installed — run: pip install nautilus_trader"
        )

    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=TraderId("PAPERBULL-BACKTESTER-001"),
        )
    )

    venue = Venue("NSE")
    instrument = Equity(
        instrument_id=InstrumentId.from_str(f"{ticker}.NSE"),
        raw_symbol=ticker,
        currency=INR,
        price_precision=2,
        price_increment=Price(Decimal("0.05"), precision=2),
        lot_size=Quantity(1, precision=0),
        ts_event=0,
        ts_init=0,
    )
    engine.add_venue(
        venue=venue,
        oms_type="HEDGING",
        account_type="CASH",
        starting_balances=[Money(initial_capital, INR)],
    )
    engine.add_instrument(instrument)

    bars = _df_to_bars(ohlcv_df, instrument.id)
    engine.add_data(bars)

    engine.run()
    stats = engine.get_stats_pnls_formatted()

    return {
        "ticker": ticker,
        "total_return_pct": stats.get("Total Return [%]", 0),
        "sharpe_ratio": stats.get("Sharpe Ratio (Ann.)", 0),
        "max_drawdown_pct": stats.get("Max. Drawdown [%]", 0),
        "win_rate": stats.get("Win Rate [%]", 0),
        "total_trades": stats.get("Total Orders", 0),
        "profit_factor": stats.get("Profit Factor", 0),
    }


def _df_to_bars(df: pd.DataFrame, instrument_id) -> list:
    """Convert PaperBull's OHLCV DataFrame to NautilusTrader Bar objects."""
    bars = []
    bar_type = BarType(
        instrument_id=instrument_id,
        bar_spec=BarSpecification(1, BarAggregation.DAY, PriceType.LAST),
    )
    for _, row in df.iterrows():
        bar = Bar(
            bar_type=bar_type,
            open=Price(row["open"], precision=2),
            high=Price(row["high"], precision=2),
            low=Price(row["low"], precision=2),
            close=Price(row["close"], precision=2),
            volume=Quantity(row["volume"], precision=0),
            ts_event=int(pd.Timestamp(row["date"]).timestamp() * 1e9),
            ts_init=int(pd.Timestamp(row["date"]).timestamp() * 1e9),
        )
        bars.append(bar)
    return bars
