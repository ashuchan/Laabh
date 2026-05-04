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


# --- F&O jobs ---

async def _fno_chain_collect_tier1() -> None:
    from src.fno.chain_collector import collect_tier
    await collect_tier(1)


async def _fno_chain_collect_tier2() -> None:
    from src.fno.chain_collector import collect_tier
    await collect_tier(2)


async def _fno_tier_refresh() -> None:
    from src.fno.tier_manager import refresh
    counts = await refresh()
    logger.info(f"fno tier refresh: {counts}")


async def _fno_issue_review_loop() -> None:
    from src.fno.issue_filer import run
    await run()


async def _fno_vix_refresh() -> None:
    from src.fno.orchestrator import run_vix_refresh
    await run_vix_refresh()


async def _fno_premarket_pipeline() -> None:
    from src.fno.orchestrator import run_premarket_pipeline
    result = await run_premarket_pipeline()
    logger.info(f"fno premarket pipeline: {result}")


async def _fno_eod_tasks() -> None:
    from src.fno.orchestrator import run_eod_tasks
    await run_eod_tasks()


async def _fno_morning_brief() -> None:
    from src.fno.orchestrator import _send_morning_brief
    await _send_morning_brief()


async def _fno_phase4_entry() -> None:
    """Auto-fire paper entries from Phase 3 PROCEED candidates."""
    from src.fno.entry_executor import auto_enter
    result = await auto_enter()
    logger.info(f"fno phase4 entry: {result}")


async def _fno_phase4_manage() -> None:
    """Mark-to-market every open paper position; close on stop/target;
    update trailing stops; emit Telegram alerts on close."""
    from src.fno.position_manager import manage_tick
    result = await manage_tick()
    if result.get("closed", 0) or result.get("trailing", 0):
        logger.info(f"fno phase4 manage: {result}")


async def _fno_phase4_hard_exit() -> None:
    """14:30 IST: force-close every still-open position with a hard-exit alert."""
    from src.fno.position_manager import hard_exit_all
    result = await hard_exit_all()
    logger.info(f"fno phase4 hard exit: {result}")


async def _fno_phase4_position_digest() -> None:
    """Send a Telegram digest of open positions with MTM P&L."""
    from src.fno.position_manager import send_position_digest
    n = await send_position_digest()
    logger.info(f"fno phase4 position digest: {n} open positions")


async def _fno_macro_collect() -> None:
    from src.collectors.macro_collector import collect
    n = await collect()
    logger.info(f"macro collector: {n} records stored")


async def _fno_fii_dii_collect() -> None:
    from src.collectors.fii_dii_collector import fetch_yesterday
    await fetch_yesterday()


async def _run_analyst_backtest_scoring() -> None:
    from src.services.analyst_scorer import compute_analyst_backtest_score_all
    results = await compute_analyst_backtest_score_all(lookback_days=90)
    logger.info(f"analyst backtest scoring: {len(results)} analysts scored")


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

    # --- F&O: pre-market macro data (06:00-09:15 IST, every 15 min) ---
    sched.add_job(
        _fno_macro_collect,
        CronTrigger(minute="0,15,30,45", hour="6-9", day_of_week="mon-fri", timezone=ist),
        id="fno_macro",
        max_instances=1,
        coalesce=True,
    )
    # 07:00 IST — Phase 1: liquidity filter
    sched.add_job(
        _fno_premarket_pipeline,
        CronTrigger(hour=7, minute=0, day_of_week="mon-fri", timezone=ist),
        id="fno_premarket",
    )
    # 06:00 IST — daily tier assignment refresh (must run before market open)
    sched.add_job(
        _fno_tier_refresh,
        CronTrigger(hour=6, minute=0, day_of_week="mon-fri", timezone=ist),
        id="fno_tier_refresh",
    )
    # 09:05 IST — VIX + ban list
    sched.add_job(
        _fno_vix_refresh,
        CronTrigger(hour=9, minute=5, day_of_week="mon-fri", timezone=ist),
        id="fno_vix_premarket",
    )
    # Tier 1: every 5 min during market hours (09:00–15:30)
    sched.add_job(
        _fno_chain_collect_tier1,
        CronTrigger(minute="*/5", hour="9-15", day_of_week="mon-fri", timezone=ist),
        id="fno_chain_collect_tier1",
        max_instances=1,
        coalesce=True,
    )
    # Tier 2: every 15 min during market hours (09:00–15:30)
    sched.add_job(
        _fno_chain_collect_tier2,
        CronTrigger(minute="0,15,30,45", hour="9-15", day_of_week="mon-fri", timezone=ist),
        id="fno_chain_collect_tier2",
        max_instances=1,
        coalesce=True,
    )
    # 18:30 IST — daily review loop (issues + Telegram summary)
    sched.add_job(
        _fno_issue_review_loop,
        CronTrigger(hour=18, minute=30, day_of_week="mon-fri", timezone=ist),
        id="fno_issue_review_loop",
    )
    # Every 5 min during market hours — VIX recheck
    sched.add_job(
        _fno_vix_refresh,
        CronTrigger(minute="*/5", hour="9-15", day_of_week="mon-fri", timezone=ist),
        id="fno_vix_intraday",
        max_instances=1,
        coalesce=True,
    )
    # 09:11 IST — morning brief (Phase 3 PROCEED summary to Telegram)
    sched.add_job(
        _fno_morning_brief,
        CronTrigger(hour=9, minute=11, day_of_week="mon-fri", timezone=ist),
        id="fno_morning_brief",
    )
    # 09:15 IST — Phase 4 entry: open paper positions for each PROCEED.
    # Runs once per day, four minutes after the morning brief, so the live
    # 09:00 chain has had time to populate fresh bid/ask via the tier-1 cron.
    sched.add_job(
        _fno_phase4_entry,
        CronTrigger(hour=9, minute=15, day_of_week="mon-fri", timezone=ist),
        id="fno_phase4_entry",
    )
    # 09:16-14:30 IST every minute — Phase 4 manage tick: mark-to-market every
    # open position, close on stop/target, update trailing stops, send alerts.
    sched.add_job(
        _fno_phase4_manage,
        CronTrigger(
            minute="*",
            hour="9-14",
            day_of_week="mon-fri",
            timezone=ist,
        ),
        id="fno_phase4_manage",
        max_instances=1,
        coalesce=True,
    )
    # 14:30 IST — Phase 4 hard exit: force-close anything still open.
    sched.add_job(
        _fno_phase4_hard_exit,
        CronTrigger(hour=14, minute=30, day_of_week="mon-fri", timezone=ist),
        id="fno_phase4_hard_exit",
    )
    # 11:30 IST — mid-session position digest to Telegram (open trades + MTM).
    sched.add_job(
        _fno_phase4_position_digest,
        CronTrigger(hour=11, minute=30, day_of_week="mon-fri", timezone=ist),
        id="fno_phase4_digest_midday",
    )
    # 13:30 IST — second position digest before the hard-exit window.
    sched.add_job(
        _fno_phase4_position_digest,
        CronTrigger(hour=13, minute=30, day_of_week="mon-fri", timezone=ist),
        id="fno_phase4_digest_afternoon",
    )
    # 15:40 IST — EOD IV history + daily summary
    sched.add_job(
        _fno_eod_tasks,
        CronTrigger(hour=15, minute=40, day_of_week="mon-fri", timezone=ist),
        id="fno_eod",
    )
    # 18:00 IST — FII/DII data (published after market close)
    sched.add_job(
        _fno_fii_dii_collect,
        CronTrigger(hour=18, minute=0, day_of_week="mon-fri", timezone=ist),
        id="fno_fii_dii",
    )

    # --- OSS integrations: analyst backtest scoring ---
    # Sunday 10:00 IST (04:30 UTC) — backtests all analyst signals from the past 90 days
    sched.add_job(
        _run_analyst_backtest_scoring,
        CronTrigger(hour=10, minute=0, day_of_week="sun", timezone=ist),
        id="analyst_backtest_scoring",
    )

    return sched
