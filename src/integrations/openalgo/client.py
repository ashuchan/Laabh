"""
OpenAlgo adapter — wraps the official MIT-licensed openalgo pip SDK.
The OpenAlgo *server* is AGPL (runs as Docker sidecar).
The *client SDK* (pip install openalgo) is MIT — safe to import here.
"""
from __future__ import annotations

import os

try:
    import zmq
    import zmq.asyncio as zmq_asyncio
except ImportError:  # pragma: no cover
    zmq = None  # type: ignore[assignment]
    zmq_asyncio = None  # type: ignore[assignment]

try:
    from openalgo import api as OpenAlgoAPI  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    OpenAlgoAPI = None  # type: ignore[assignment]

OPENALGO_API_KEY = os.environ.get("OPENALGO_API_KEY", "")
OPENALGO_HOST = os.environ.get("OPENALGO_HOST", "http://localhost:5000")
OPENALGO_WS_URL = os.environ.get("OPENALGO_WS_URL", "ws://localhost:8765")

_client = None


def _get_client():
    global _client
    if _client is None:
        if OpenAlgoAPI is None:
            raise RuntimeError("openalgo package is not installed — run: pip install openalgo")
        _client = OpenAlgoAPI(api_key=OPENALGO_API_KEY, host=OPENALGO_HOST)
    return _client


async def health() -> dict:
    """Return integration health status."""
    try:
        resp = _get_client().funds()
        if resp.get("status") == "success":
            return {"status": "ok", "broker": resp.get("data", {}).get("broker")}
    except Exception as e:
        return {"status": "down", "error": str(e)}
    return {"status": "degraded"}


def get_ltp(symbol: str, exchange: str = "NSE") -> float:
    """Get last traded price via OpenAlgo unified quote API."""
    resp = _get_client().quotes(symbol=symbol, exchange=exchange)
    return float(resp["data"]["ltp"])


def place_paper_order(
    symbol: str,
    exchange: str,
    action: str,
    quantity: int,
    price_type: str = "MARKET",
) -> dict:
    """
    Route paper trade through OpenAlgo sandbox.
    OpenAlgo sandbox is activated when BROKER=sandbox in its .env.
    """
    return _get_client().placeorder(
        symbol=symbol,
        exchange=exchange,
        action=action,
        quantity=quantity,
        price_type=price_type,
        product="MIS",
    )


class ZMQTickSubscriber:
    """
    Subscribe to OpenAlgo's normalized ZMQ tick feed.
    Replaces direct Angel One WebSocket in price-fetcher service.
    """

    def __init__(self, zmq_url: str = "tcp://openalgo:5556") -> None:
        if zmq is None:
            raise RuntimeError("pyzmq is not installed — run: pip install pyzmq")
        self._ctx = zmq_asyncio.Context()
        self._sock = self._ctx.socket(zmq.SUB)
        self._sock.connect(zmq_url)

    def subscribe(self, *symbols: str) -> None:
        """Subscribe to tick stream for one or more symbols."""
        for sym in symbols:
            self._sock.setsockopt_string(zmq.SUBSCRIBE, sym)

    async def stream(self):
        """Yield normalized tick dicts: {symbol, ltp, volume, timestamp}"""
        while True:
            msg = await self._sock.recv_json()
            yield msg

    def close(self) -> None:
        """Shut down ZMQ socket and context."""
        self._sock.close()
        self._ctx.term()
