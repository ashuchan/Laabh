"""APScheduler job registry — Phase 1+2+3 jobs."""
from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger
from pytz import timezone as tz

from src.analytics.analyst_tracker import AnalystTracker
from src.analytics.convergence import ConvergenceEngine
from src.analytics.reports import ReportGenerator
from src.analytics.signal_resolver import SignalResolver
from src.analytics.source_scorer import SourceScorer
from src.collectors.bse_scraper import BSEScraperCollector
from src.collectors.google_news import GoogleNewsCollector
from src.collectors.nse_scraper import NSEScraperCollector
from src.collectors.rss_collector import RSSCollector
from src.collectors.yahoo_finance import YahooFinanceCollector
from src.config import get_settings
from src.extraction.llm_extractor import LLMExtractor
from src.services.notification_service import NotificationService
from src.services.price_service import PriceService
from src.services.signal_service import SignalService
from src.trading.order_book import OrderBook
from src.trading.portfolio_manager import PortfolioManager


# --- Phase 1 jobs ---

async def _run_rss() -> None:
    await RSSCollector().run()


async def _run_google_news() -> None:
    await GoogleNewsCollector().run()


async def _run_bse() -> None:
    await BSEScraperCollector().run()


async def _run_nse() -> None:
    await NSEScraperCollector().run()


async def _run_yahoo_eod() -> None:
    await YahooFinanceCollector(days=1).run()


async def _run_extractor() -> None:
    n = await LLMExtractor().process_pending(limit=20)
    logger.info(f"extractor: created {n} signals")


async def _run_signal_notifications() -> None:
    await SignalService().notify_watchlist_signals(since_minutes=10)


async def _run_price_alerts() -> None:
    await PriceService().check_price_alerts()


async def _run_notification_push() -> None:
    await NotificationService().push_pending()


async def _run_resolve_expired() -> None:
    await SignalService().resolve_expired()


# --- Phase 2 jobs ---

async def _run_resolve_signals() -> None:
    n = await SignalResolver().resolve_active_signals()
    logger.info(f"signal resolver: {n} resolved")


async def _run_update_portfolio_values() -> None:
    await PortfolioManager().update_all_portfolios()


async def _run_check_pending_orders() -> None:
    n = await OrderBook().check_pending_orders()
    if n:
        logger.info(f"order book: {n} orders executed")


async def _run_daily_snapshot() -> None:
    from sqlalchemy import select
    from src.db import session_scope
    from src.models.portfolio import Portfolio
    pm = PortfolioManager()
    async with session_scope() as session:
        result = await session.execute(
            select(Portfolio).where(Portfolio.is_active == True)
        )
        portfolios = result.scalars().all()
    for p in portfolios:
        try:
            await pm.take_snapshot(p.id)
        except Exception as exc:
            logger.error(f"snapshot failed for {p.id}: {exc}")


async def _run_update_analyst_scores() -> None:
    n = await AnalystTracker().update_all_scores()
    await SourceScorer().update_all_source_scores()
    logger.info(f"analyst scores updated: {n}")


async def _run_daily_report() -> None:
    await ReportGenerator().send_daily_report()


# --- Phase 3 jobs ---

async def _start_live_recorders() -> None:
    from src.whisper_pipeline.stream_recorder import StreamRecorder
    await StreamRecorder().start_all()


async def _stop_live_recorders() -> None:
    from src.whisper_pipeline.stream_recorder import StreamRecorder
    await StreamRecorder().stop_all()


async def _process_whisper_chunks() -> None:
    from src.whisper_pipeline.pipeline import WhisperPipeline
    await WhisperPipeline().process_pending_chunks()


async def _batch_vod_transcription() -> None:
    from src.whisper_pipeline.pipeline import WhisperPipeline
    await WhisperPipeline().run_batch_vod()


async def _run_convergence_check() -> None:
    n = await ConvergenceEngine().run_convergence_check()
    logger.info(f"convergence check: {n} instruments updated")


def build_scheduler() -> AsyncIOScheduler:
    """Create and configure the full APScheduler instance (not yet started)."""
    settings = get_settings()
    ist = tz(settings.timezone)
    sched = AsyncIOScheduler(timezone=ist)

    # --- Phase 1: Data collection ---
    sched.add_job(_run_rss, IntervalTrigger(minutes=5), id="rss", max_instances=1, coalesce=True)
    sched.add_job(
        _run_google_news, IntervalTrigger(minutes=10), id="gnews", max_instances=1, coalesce=True
    )
    sched.add_job(_run_bse, IntervalTrigger(minutes=3), id="bse", max_instances=1, coalesce=True)
    sched.add_job(_run_nse, IntervalTrigger(minutes=5), id="nse", max_instances=1, coalesce=True)
    sched.add_job(
        _run_extractor, IntervalTrigger(minutes=2), id="extract", max_instances=1, coalesce=True
    )
    sched.add_job(
        _run_signal_notifications,
        IntervalTrigger(minutes=1),
        id="notify_signals",
        max_instances=1,
    )
    sched.add_job(
        _run_price_alerts,
        IntervalTrigger(seconds=30),
        id="price_alerts",
        max_instances=1,
    )
    sched.add_job(
        _run_notification_push,
        IntervalTrigger(seconds=30),
        id="push_notifications",
        max_instances=1,
    )
    sched.add_job(
        _run_yahoo_eod,
        CronTrigger(hour=18, minute=0, day_of_week="mon-fri", timezone=ist),
        id="yahoo_eod",
    )
    sched.add_job(
        _run_resolve_expired,
        CronTrigger(minute=0, hour="9-15", day_of_week="mon-fri", timezone=ist),
        id="resolve_expired",
    )

    # --- Phase 2: Trading engine + analytics ---
    sched.add_job(
        _run_resolve_signals,
        IntervalTrigger(minutes=30),
        id="resolve_signals",
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        _run_update_portfolio_values,
        IntervalTrigger(minutes=5),
        id="update_portfolio",
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        _run_check_pending_orders,
        IntervalTrigger(minutes=1),
        id="check_orders",
        max_instances=1,
        coalesce=True,
    )
    # 15:35 IST — daily snapshot after market close
    sched.add_job(
        _run_daily_snapshot,
        CronTrigger(hour=15, minute=35, day_of_week="mon-fri", timezone=ist),
        id="daily_snapshot",
    )
    # 18:00 IST — update analyst scores
    sched.add_job(
        _run_update_analyst_scores,
        CronTrigger(hour=18, minute=0, day_of_week="mon-fri", timezone=ist),
        id="update_analyst_scores",
    )
    # 18:30 IST — daily Telegram report
    sched.add_job(
        _run_daily_report,
        CronTrigger(hour=18, minute=30, day_of_week="mon-fri", timezone=ist),
        id="daily_report",
    )

    # --- Phase 3: Whisper pipeline ---
    # WHISPER_MODEL is the feature flag — when unset, skip the entire pipeline.
    if settings.whisper_model:
        # 9:10 IST — start live recorders before market open
        sched.add_job(
            _start_live_recorders,
            CronTrigger(hour=9, minute=10, day_of_week="mon-fri", timezone=ist),
            id="start_recorders",
        )
        # 15:35 IST — stop live recorders
        sched.add_job(
            _stop_live_recorders,
            CronTrigger(hour=15, minute=35, day_of_week="mon-fri", timezone=ist),
            id="stop_recorders",
        )
        sched.add_job(
            _process_whisper_chunks,
            IntervalTrigger(minutes=2),
            id="process_whisper",
            max_instances=1,
            coalesce=True,
        )
        # 16:00 IST — batch VOD transcription after market close
        sched.add_job(
            _batch_vod_transcription,
            CronTrigger(hour=16, minute=0, day_of_week="mon-fri", timezone=ist),
            id="batch_vod",
        )
        sched.add_job(
            _run_convergence_check,
            IntervalTrigger(minutes=15),
            id="convergence",
            max_instances=1,
            coalesce=True,
        )
    else:
        logger.info("whisper pipeline disabled (WHISPER_MODEL unset)")

    return sched
