"""FastAPI application — REST + WebSocket for real-time price streaming."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from src.api.middleware import RateLimitMiddleware, http_exception_handler
from src.api.routes import analysts, fno, instruments, portfolio, reports, signals, trades, watchlist
from src.config import get_settings
from src.db import dispose_engine
from src.scheduler import build_scheduler

# Active WebSocket connections: symbol → set of websockets
_price_subscribers: dict[str, set[WebSocket]] = {}
_scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
    _scheduler = build_scheduler()
    _scheduler.start()
    logger.info("Laabh API started")
    yield
    if _scheduler:
        _scheduler.shutdown(wait=False)
    await dispose_engine()
    logger.info("Laabh API shutdown")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Laabh API",
        description="Paper trading API for Indian stock markets",
        version="2.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RateLimitMiddleware)

    app.include_router(trades.router)
    app.include_router(portfolio.router)
    app.include_router(signals.router)
    app.include_router(watchlist.router)
    app.include_router(analysts.router)
    app.include_router(instruments.router)
    app.include_router(fno.router)
    app.include_router(reports.router)

    app.add_exception_handler(Exception, http_exception_handler)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.websocket("/ws/prices")
    async def ws_prices(websocket: WebSocket):
        """Real-time price streaming WebSocket.

        Clients send: {"action": "subscribe", "symbols": ["RELIANCE", "TCS"]}
        Server sends: {"symbol": "RELIANCE", "ltp": 2456.30, "change_pct": 1.2}
        """
        await websocket.accept()
        subscribed: set[str] = set()
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                    msg = json.loads(raw)
                    action = msg.get("action")
                    symbols = msg.get("symbols", [])

                    if action == "subscribe":
                        for sym in symbols:
                            if sym not in _price_subscribers:
                                _price_subscribers[sym] = set()
                            _price_subscribers[sym].add(websocket)
                            subscribed.add(sym)
                        await websocket.send_text(
                            json.dumps({"type": "subscribed", "symbols": list(subscribed)})
                        )

                    elif action == "unsubscribe":
                        for sym in symbols:
                            if sym in _price_subscribers:
                                _price_subscribers[sym].discard(websocket)
                            subscribed.discard(sym)

                except asyncio.TimeoutError:
                    # Send heartbeat to keep connection alive
                    await websocket.send_text(json.dumps({"type": "ping"}))

        except WebSocketDisconnect:
            pass
        finally:
            for sym in subscribed:
                if sym in _price_subscribers:
                    _price_subscribers[sym].discard(websocket)

    return app


async def broadcast_price(symbol: str, ltp: float, change_pct: float) -> None:
    """Called by Angel One WebSocket handler to push ticks to mobile clients."""
    if symbol not in _price_subscribers:
        return
    payload = json.dumps({"symbol": symbol, "ltp": ltp, "change_pct": change_pct})
    dead: set[WebSocket] = set()
    for ws in list(_price_subscribers[symbol]):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.add(ws)
    for ws in dead:
        _price_subscribers[symbol].discard(ws)


app = create_app()
