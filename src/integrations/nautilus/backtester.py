"""
NautilusTrader backtesting adapter.
LGPL-3.0 — imported as library, strategies stay private.
"""
from __future__ import annotations

import logging
from decimal import Decimal

import pandas as pd

logger = logging.getLogger(__name__)

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


if _NAUTILUS_AVAILABLE:
    from nautilus_trader.common.actor import Actor  # type: ignore[import-not-found]
    from nautilus_trader.trading.strategy import Strategy  # type: ignore[import-not-found]
    from nautilus_trader.config import StrategyConfig  # type: ignore[import-not-found]
    from nautilus_trader.model.enums import OrderSide  # type: ignore[import-not-found]

    class _SignalReplayActor(Strategy):
        """
        Replays a list of signals as market orders.
        Each signal {date, direction} becomes a BUY or SELL market order
        at the bar closest to that date.
        """

        def __init__(self, signal_history: list[dict], instrument_id) -> None:
            super().__init__(config=StrategyConfig())
            self._signals = signal_history
            self._instrument_id = instrument_id

        def on_start(self) -> None:
            self.subscribe_bars(
                BarType(
                    instrument_id=self._instrument_id,
                    bar_spec=BarSpecification(1, BarAggregation.DAY, PriceType.LAST),
                )
            )

        def on_bar(self, bar: Bar) -> None:
            bar_date = pd.Timestamp(bar.ts_event, unit="ns").date().isoformat()
            for sig in self._signals:
                if sig.get("date") == bar_date:
                    side = (
                        OrderSide.BUY
                        if str(sig.get("direction", "")).upper() == "BUY"
                        else OrderSide.SELL
                    )
                    order = self.order_factory.market(
                        instrument_id=self._instrument_id,
                        order_side=side,
                        quantity=Quantity(1, precision=0),
                    )
                    self.submit_order(order)

else:
    class _SignalReplayActor:  # type: ignore[no-redef]
        """Stub class so the module imports cleanly when nautilus is absent."""

        def __init__(self, *args, **kwargs) -> None:
            pass


async def health() -> dict:
    """Return integration health status."""
    return {
        "status": "ok" if _NAUTILUS_AVAILABLE else "down",
        "backend": "nautilustrader",
        "available": _NAUTILUS_AVAILABLE,
    }


_NULL_BACKTEST_RESULT: dict = {
    "total_return_pct": None,
    "sharpe_ratio": None,
    "max_drawdown_pct": None,
    "win_rate": None,
    "total_trades": 0,
    "profit_factor": None,
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
        signal_history: list of {date, ticker, direction, entry, target, stop}.
                        Each signal becomes a market order at its signal date.
        ohlcv_df:       DataFrame with columns: date, open, high, low, close, volume.
        ticker:         NSE symbol e.g. "RELIANCE"
        initial_capital: starting capital in INR (default ₹10L)

    Returns:
        Performance metrics dict. On any failure, returns _NULL_BACKTEST_RESULT
        with an "error" key — never raises.
    """
    if not _NAUTILUS_AVAILABLE:
        logger.warning(
            "run_signal_backtest: nautilus_trader not installed — "
            "returning null result. Run: pip install nautilus_trader"
        )
        return {**_NULL_BACKTEST_RESULT, "ticker": ticker,
                "error": "nautilus_trader not installed"}

    if not signal_history:
        logger.warning(f"run_signal_backtest: empty signal_history for {ticker}")
        return {**_NULL_BACKTEST_RESULT, "ticker": ticker,
                "error": "empty signal_history"}

    if ohlcv_df.empty:
        logger.warning(f"run_signal_backtest: empty ohlcv_df for {ticker}")
        return {**_NULL_BACKTEST_RESULT, "ticker": ticker,
                "error": "empty ohlcv_df"}

    try:
        return _run_backtest_impl(signal_history, ohlcv_df, ticker, initial_capital)
    except Exception as exc:
        logger.error(
            f"run_signal_backtest: engine failed for {ticker}: {exc}",
            exc_info=True,
        )
        return {**_NULL_BACKTEST_RESULT, "ticker": ticker, "error": str(exc)}


def _run_backtest_impl(
    signal_history: list[dict],
    ohlcv_df: pd.DataFrame,
    ticker: str,
    initial_capital: float,
) -> dict:
    """Internal implementation — called only when nautilus is available."""
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
    if not bars:
        return {**_NULL_BACKTEST_RESULT, "ticker": ticker,
                "error": "no valid bars after filtering NaT rows"}
    engine.add_data(bars)

    # Add signal replay actor so the engine actually trades
    # Without this, the engine runs empty and all stats are zero.
    actor = _SignalReplayActor(signal_history, instrument.id)
    engine.add_strategy(actor)

    engine.run()
    stats = engine.get_stats_pnls_formatted()

    return {
        "ticker": ticker,
        "total_return_pct": stats.get("Total Return [%]", None),
        "sharpe_ratio": stats.get("Sharpe Ratio (Ann.)", None),
        "max_drawdown_pct": stats.get("Max. Drawdown [%]", None),
        "win_rate": stats.get("Win Rate [%]", None),
        "total_trades": stats.get("Total Orders", 0),
        "profit_factor": stats.get("Profit Factor", None),
    }


def _df_to_bars(df: pd.DataFrame, instrument_id) -> list:
    """
    Convert PaperBull OHLCV DataFrame to NautilusTrader Bar objects.

    Uses Decimal for price values (required by NautilusTrader 1.200+).
    Skips rows with NaT dates or zero/negative prices with a warning log.
    """
    bars = []
    bar_type = BarType(
        instrument_id=instrument_id,
        bar_spec=BarSpecification(1, BarAggregation.DAY, PriceType.LAST),
    )
    for idx, row in df.iterrows():
        ts = pd.Timestamp(row["date"]) if "date" in row.index else pd.Timestamp(idx)
        if pd.isna(ts):
            logger.warning(f"_df_to_bars: skipping NaT row at index {idx}")
            continue
        try:
            bar = Bar(
                bar_type=bar_type,
                open=Price(Decimal(str(round(float(row["open"]), 2))), precision=2),
                high=Price(Decimal(str(round(float(row["high"]), 2))), precision=2),
                low=Price(Decimal(str(round(float(row["low"]), 2))), precision=2),
                close=Price(Decimal(str(round(float(row["close"]), 2))), precision=2),
                volume=Quantity(max(int(row["volume"]), 1), precision=0),
                ts_event=int(ts.timestamp() * 1e9),
                ts_init=int(ts.timestamp() * 1e9),
            )
            bars.append(bar)
        except Exception as exc:
            logger.warning(f"_df_to_bars: skipping row {idx} — {exc}")
            continue
    return bars
