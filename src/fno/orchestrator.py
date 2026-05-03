"""F&O Orchestrator — coordinates the daily F&O pipeline (Phases 1-4).

Execution flow (all IST times, market days Mon-Fri):
  07:00 — Phase 1: Liquidity filter (universe.run_phase1)
  07:15 — Phase 2: Catalyst scoring (catalyst_scorer.run_phase2)
  07:30 — Phase 3: Thesis synthesis (thesis_synthesizer.run_phase3)
  09:00 — Pre-market: Collect chain snapshots (chain_collector.collect_all)
  09:05 — Pre-market: VIX + ban list refresh
  09:15 — Market open: Phase 4 intraday manager starts
  14:30 — Hard exit: force-close all positions
  15:40 — EOD: IV history builder, daily summary notification
  18:30 — Post-market: FII/DII data collection

The orchestrator is stateless — it holds no mutable state itself.
Intraday position state is held in-memory by a global IntradayState instance
and periodically checkpointed to `fno_signals` table.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from loguru import logger

from src.config import Settings
from src.fno.ban_list import fetch_today as fetch_ban_list
from src.fno.catalyst_scorer import run_phase2
from src.fno.chain_collector import collect_all as collect_chains
from src.fno.iv_history_builder import build_for_date
from src.fno.thesis_synthesizer import run_phase3
from src.fno.universe import run_phase1
from src.fno.vix_collector import run_once as collect_vix

_settings = Settings()


async def run_premarket_pipeline(
    run_date: date | None = None,
    *,
    as_of: "datetime | None" = None,
) -> dict:
    """Run Phases 1-3 and return summary counts."""
    if run_date is None:
        run_date = date.today() if as_of is None else as_of.date()

    if not _settings.fno_module_enabled:
        logger.info("fno.orchestrator: F&O module disabled — skipping premarket pipeline")
        return {"skipped": True}

    logger.info(f"fno.orchestrator: starting premarket pipeline for {run_date}")

    # Phase 1: liquidity filter — pass as_of so chain queries are bounded to the replay time
    phase1_results = await run_phase1(run_date, as_of=as_of)
    phase1_passed = sum(1 for r in phase1_results if r.passed)
    logger.info(f"fno.orchestrator: Phase 1 → {phase1_passed}/{len(phase1_results)} passed")

    # Phase 2: catalyst scoring
    phase2_results = await run_phase2(run_date)
    phase2_passed = sum(1 for r in phase2_results if r.passed)
    logger.info(f"fno.orchestrator: Phase 2 → {phase2_passed}/{len(phase2_results)} passed")

    # Phase 3: thesis synthesis
    phase3_results = await run_phase3(run_date)
    phase3_proceed = sum(1 for r in phase3_results if r.decision == "PROCEED")
    logger.info(f"fno.orchestrator: Phase 3 → {phase3_proceed} PROCEED decisions")

    return {
        "run_date": run_date.isoformat(),
        "phase1_total": len(phase1_results),
        "phase1_passed": phase1_passed,
        "phase2_total": len(phase2_results),
        "phase2_passed": phase2_passed,
        "phase3_total": len(phase3_results),
        "phase3_proceed": phase3_proceed,
    }


async def run_chain_refresh() -> int:
    """Collect fresh option chain snapshots for all F&O instruments."""
    if not _settings.fno_module_enabled:
        return 0
    count = await collect_chains()
    logger.info(f"fno.orchestrator: chain refresh stored {count} snapshots")
    return count


async def run_vix_refresh() -> None:
    """Refresh India VIX reading and F&O ban list."""
    if not _settings.fno_module_enabled:
        return
    try:
        await collect_vix()
    except Exception as exc:
        logger.warning(f"fno.orchestrator: VIX refresh failed: {exc}")
    try:
        await fetch_ban_list()
    except Exception as exc:
        logger.warning(f"fno.orchestrator: ban list refresh failed: {exc}")


async def run_eod_tasks(run_date: date | None = None) -> None:
    """Post-market tasks: IV history builder + daily summary notification."""
    if run_date is None:
        run_date = date.today()
    if not _settings.fno_module_enabled:
        return

    try:
        upserted = await build_for_date(run_date)
        logger.info(f"fno.orchestrator: IV history built for {upserted} instruments")
    except Exception as exc:
        logger.warning(f"fno.orchestrator: IV history builder failed: {exc}")

    try:
        await _send_daily_summary(run_date)
    except Exception as exc:
        logger.warning(f"fno.orchestrator: daily summary failed: {exc}")


async def _send_daily_summary(run_date: date) -> None:
    from src.fno.notifications import format_daily_summary
    from src.services.side_effect_gateway import get_gateway

    summary_msg = format_daily_summary(
        run_date=run_date.isoformat(),
        phase1_passed=0,
        phase2_passed=0,
        phase3_proceed=0,
        trades_entered=0,
        net_pnl=Decimal("0"),
    )
    await get_gateway().send_telegram(summary_msg)
    logger.info("fno.orchestrator: daily summary notification sent")
