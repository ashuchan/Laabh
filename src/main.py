"""Entry point — runs the scheduler and Angel One WebSocket stream concurrently.

When run as a Windows service via NSSM, the supervisor sends ``CTRL_BREAK_EVENT``
on stop (NSSM ``AppStopMethodConsole``). The handlers below trip the same async
``stop_event`` for SIGBREAK / SIGINT / SIGTERM so jobs in flight finish cleanly
before the process exits — matching the ``AppStopMethodConsole`` grace window
configured by ``scripts/install_service.ps1``.
"""
from __future__ import annotations

import asyncio
import signal
import sys

from loguru import logger

from src.collectors.angel_one import AngelOneCollector
from src.config import get_settings
from src.db import dispose_engine
from src.scheduler import build_scheduler
from src.scheduler_reconciler import reconcile_missed


def _configure_logging() -> None:
    settings = get_settings()
    logger.remove()
    logger.add(sys.stderr, level=settings.log_level, backtrace=False, diagnose=False)


def _shutdown_signals() -> tuple[int, ...]:
    """Signals that should trigger graceful shutdown across platforms.

    Windows: SIGINT (Ctrl+C) + SIGBREAK (Ctrl+Break, what NSSM sends).
    POSIX:   SIGINT + SIGTERM.

    SIGTERM is intentionally skipped on Windows: Python exposes the constant
    but the OS never delivers it (Stop-Service / nssm stop send Ctrl+Break,
    then WM_CLOSE, then TerminateProcess), so registering for it is dead
    code that only adds noise to the log.
    """
    sigs: list[int] = [signal.SIGINT]
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        sigs.append(sigbreak)
    if sys.platform != "win32" and hasattr(signal, "SIGTERM"):
        sigs.append(signal.SIGTERM)
    return tuple(sigs)


async def _run() -> None:
    _configure_logging()
    settings = get_settings()
    mode_tag = "[QUANT]" if settings.laabh_intraday_mode == "quant" else "[AGENTIC]"
    logger.info(f"Laabh starting — intraday mode: {mode_tag}")

    scheduler = build_scheduler()
    scheduler.start()

    # Catch up daily-critical jobs whose firing time was missed while the
    # scheduler was offline. Runs once per startup, after the scheduler is
    # live so reconcile_missed can schedule one-shot DateTrigger jobs.
    try:
        await reconcile_missed(scheduler)
    except Exception as exc:
        logger.error(f"reconciler failed: {exc!r}")

    settings = get_settings()
    angel: AngelOneCollector | None = None
    stream_task: asyncio.Task[None] | None = None
    if settings.angel_one_enabled:
        angel = AngelOneCollector()
        stream_task = asyncio.create_task(angel.run_stream(), name="angel_one_ws")
    else:
        logger.info("Angel One disabled (ANGEL_ONE_ENABLED=false) — WebSocket stream not started")

    stop_event = asyncio.Event()

    def _on_signal(signum: int) -> None:
        name = signal.Signals(signum).name if signum in [s.value for s in signal.Signals] else str(signum)
        logger.info(f"shutdown signal received: {name}")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in _shutdown_signals():
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except NotImplementedError:
            # asyncio on Windows ProactorEventLoop doesn't support
            # add_signal_handler — fall back to signal.signal so SIGBREAK
            # from NSSM still trips the stop_event.
            signal.signal(sig, lambda signum, _frame: loop.call_soon_threadsafe(_on_signal, signum))

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("stopping scheduler + stream (draining in-flight jobs)")
        # wait=True drains running jobs; bounded by NSSM AppStopMethodConsole.
        scheduler.shutdown(wait=True)
        if angel is not None:
            await angel.stop()
        if stream_task is not None:
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
