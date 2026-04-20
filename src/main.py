"""Entry point — runs the scheduler and Angel One WebSocket stream concurrently."""
from __future__ import annotations

import asyncio
import signal
import sys

from loguru import logger

from src.collectors.angel_one import AngelOneCollector
from src.config import get_settings
from src.db import dispose_engine
from src.scheduler import build_scheduler


def _configure_logging() -> None:
    settings = get_settings()
    logger.remove()
    logger.add(sys.stderr, level=settings.log_level, backtrace=False, diagnose=False)


async def _run() -> None:
    _configure_logging()
    logger.info("Laabh Phase 1 starting")

    scheduler = build_scheduler()
    scheduler.start()

    angel = AngelOneCollector()
    stream_task = asyncio.create_task(angel.run_stream(), name="angel_one_ws")

    stop_event = asyncio.Event()

    def _on_signal() -> None:
        logger.info("shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            # Windows: fall back to KeyboardInterrupt
            pass

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("stopping scheduler + stream")
        scheduler.shutdown(wait=False)
        await angel.stop()
        stream_task.cancel()
        try:
            await stream_task
        except (asyncio.CancelledError, Exception):
            pass
        await dispose_engine()
        logger.info("bye")


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
