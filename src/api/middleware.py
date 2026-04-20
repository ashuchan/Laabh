"""CORS, rate limiting, and error handling middleware."""
from __future__ import annotations

import time
from collections import defaultdict

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple in-process rate limiter: max 60 requests/minute per IP."""

    LIMIT = 60
    WINDOW = 60  # seconds

    def __init__(self, app) -> None:
        super().__init__(app)
        self._counts: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next) -> Response:
        client_ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        window_start = now - self.WINDOW

        timestamps = self._counts[client_ip]
        # Prune old timestamps
        self._counts[client_ip] = [t for t in timestamps if t > window_start]
        if len(self._counts[client_ip]) >= self.LIMIT:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Max 60 requests/minute."},
            )
        self._counts[client_ip].append(now)
        return await call_next(request)


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all error handler that returns JSON with a detail field."""
    logger.error(f"unhandled error on {request.url}: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )
