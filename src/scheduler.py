"""APScheduler job registry — RSS polling, extraction, notifications, EOD prices."""
from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger
from pytz import timezone as tz

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


def build_scheduler() -> AsyncIOScheduler:
    """Create and configure the APScheduler instance (not yet started)."""
    settings = get_settings()
    sched = AsyncIOScheduler(timezone=tz(settings.timezone))

    sched.add_job(_run_rss, IntervalTrigger(minutes=5), id="rss", max_instances=1, coalesce=True)
    sched.add_job(_run_google_news, IntervalTrigger(minutes=10), id="gnews", max_instances=1, coalesce=True)
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

    # End-of-day yfinance backfill (6 PM IST, Mon–Fri)
    sched.add_job(
        _run_yahoo_eod,
        CronTrigger(hour=18, minute=0, day_of_week="mon-fri", timezone=tz(settings.timezone)),
        id="yahoo_eod",
    )
    # Resolve expired signals hourly during market hours
    sched.add_job(
        _run_resolve_expired,
        CronTrigger(minute=0, hour="9-15", day_of_week="mon-fri", timezone=tz(settings.timezone)),
        id="resolve_expired",
    )

    return sched
