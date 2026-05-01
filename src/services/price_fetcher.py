"""
Price fetcher service — routes tick stream through OpenAlgo ZMQ feed.
Replaces direct Angel One WebSocket subscription.
"""
from __future__ import annotations

from loguru import logger

from src.integrations.openalgo.client import ZMQTickSubscriber


async def run_price_fetcher(symbols: list[str], zmq_url: str = "tcp://openalgo:5556") -> None:
    """
    Subscribe to OpenAlgo's normalized ZMQ tick feed and process each tick.

    Args:
        symbols: NSE symbols to subscribe to
        zmq_url: ZMQ publisher URL from OpenAlgo container
    """
    sub = ZMQTickSubscriber(zmq_url=zmq_url)
    sub.subscribe(*symbols)
    logger.info(f"price_fetcher: subscribed to {len(symbols)} symbols via ZMQ")

    try:
        async for tick in sub.stream():
            await _process_tick(tick)
    finally:
        sub.close()
        logger.info("price_fetcher: ZMQ subscriber closed")


async def _process_tick(tick: dict) -> None:
    """Persist tick to DB and check price alerts for the updated symbol."""
    from src.services.price_service import PriceService

    symbol = tick.get("symbol", "")
    ltp = tick.get("ltp")
    if not symbol or ltp is None:
        logger.debug(f"price_fetcher: skipping malformed tick: {tick}")
        return

    logger.debug(f"tick: {symbol} @ {ltp}")
    try:
        svc = PriceService()
        await svc.update_ltp(symbol, float(ltp))
        await svc.check_price_alerts(symbol)
    except AttributeError:
        # PriceService does not yet have update_ltp — log and continue.
        # TODO: wire update_ltp(symbol, ltp) into PriceService when implemented.
        logger.warning(
            f"price_fetcher: PriceService.update_ltp not implemented — "
            f"tick for {symbol}@{ltp} not persisted"
        )
    except Exception as exc:
        logger.error(f"price_fetcher: failed to process tick for {symbol}: {exc}")
