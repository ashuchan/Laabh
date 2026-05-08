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

    # Phase 1.5: refresh the market-sentiment row Phase 2 will read. Failures
    # here are non-fatal — Phase 2 falls back to neutral 5.0 if no row is
    # found, so this is a "freshen-if-possible" step rather than a gate.
    try:
        from src.collectors.sentiment_collector import run_once as run_sentiment
        await run_sentiment(as_of=as_of)
    except Exception as exc:
        logger.warning(f"fno.orchestrator: sentiment refresh failed: {exc}")

    # Phase 2: catalyst scoring
    phase2_results = await run_phase2(run_date)
    phase2_passed = sum(1 for r in phase2_results if r.passed)
    logger.info(f"fno.orchestrator: Phase 2 → {phase2_passed}/{len(phase2_results)} passed")

    # Phase 2.5: per-symbol news fan-in for Phase 3 context.
    # Pulls a targeted Google News query for the top-N Phase 2 passers,
    # immediately runs the LLM extractor over the new articles so that
    # Phase 3's prompt has stock-specific narrative.
    try:
        from src.extraction.llm_extractor import LLMExtractor
        from src.fno.news_fanin import fan_in_for_phase2
        fanin = await fan_in_for_phase2(
            run_date,
            top_n=_settings.fno_phase3_target_output,
        )
        if fanin.get("articles", 0) > 0:
            n_signals = await LLMExtractor().process_pending(
                limit=fanin["articles"] + 10
            )
            logger.info(
                f"fno.orchestrator: phase2.5 fan-in → "
                f"{fanin['articles']} articles, {n_signals} signals"
            )
    except Exception as exc:
        logger.warning(f"fno.orchestrator: phase2.5 fan-in failed: {exc}")

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
    """15:40 IST EOD message — unified equity + F&O P&L view.

    Replaces the old phase-counts-and-net-pnl-only digest with the combined
    snapshot from ``pnl_aggregator``: per-strategy bucket P&L (equity LLM,
    FNO directional/spread/volatility), realized vs unrealized split, every
    closed trade today (winners + losers), pipeline context (P1/P2/P3
    counts), and the capital pool position. The message is sent with legacy
    Markdown so the per-bucket code-block table renders monospaced.
    """
    from src.services.pnl_aggregator import daily_pnl_snapshot
    from src.services.report_formatter import format_combined_eod_report
    from src.services.side_effect_gateway import get_gateway

    snap = await daily_pnl_snapshot(today=run_date)
    summary_msg = format_combined_eod_report(
        snap, title=f"Laabh EOD — {run_date:%d %b %Y}"
    )
    await get_gateway().send_telegram(summary_msg, parse_mode="Markdown")
    logger.info(
        f"fno.orchestrator: daily summary sent — pnl={snap.day_pnl_total:,.2f} "
        f"(realized={snap.realized_pnl_total:,.2f}, "
        f"open={snap.unrealized_pnl_total:,.2f}) p1={snap.fno_phase1_passed} "
        f"p2={snap.fno_phase2_passed} p3_proceed={snap.fno_phase3_proceed}"
    )


async def _send_morning_brief(run_date: date | None = None) -> None:
    """Pre-open brief: list every Phase 3 PROCEED candidate for today.

    Pushes to Telegram and stamps a `notifications` row so the runday
    `checkpoint.morning_brief` check finds it.
    """
    from datetime import datetime as _dt

    from sqlalchemy import select

    from src.db import session_scope
    from src.fno.notifications import format_morning_brief
    from src.models.fno_candidate import FNOCandidate
    from src.models.instrument import Instrument
    from src.models.notification import Notification
    from src.services.side_effect_gateway import get_gateway

    if run_date is None:
        run_date = date.today()

    if not _settings.fno_module_enabled:
        logger.info("fno.orchestrator: F&O disabled — skipping morning brief")
        return

    async with session_scope() as session:
        rows = await session.execute(
            select(
                FNOCandidate.instrument_id,
                Instrument.symbol,
                FNOCandidate.composite_score,
                FNOCandidate.iv_regime,
                FNOCandidate.oi_structure,
                FNOCandidate.llm_thesis,
            )
            .join(Instrument, Instrument.id == FNOCandidate.instrument_id)
            .where(
                FNOCandidate.run_date == run_date,
                FNOCandidate.phase == 3,
                FNOCandidate.llm_decision == "PROCEED",
                FNOCandidate.dryrun_run_id.is_(None),
            )
            .order_by(FNOCandidate.composite_score.desc().nulls_last())
        )
        phase3_rows = rows.all()

    # Pull contract-level proposals from the entry engine (chooses strategy +
    # specific strikes from the live chain). Map them by instrument_id so
    # we can splice them into each candidate dict below.
    from src.fno.entry_engine import propose_entries
    proposals_by_inst: dict[str, "object"] = {}
    try:
        proposals = await propose_entries(run_date)
        proposals_by_inst = {p.instrument_id: p for p in proposals}
    except Exception as exc:
        logger.warning(f"morning brief: entry_engine failed: {exc}")

    candidates = []
    for r in phase3_rows:
        prop = proposals_by_inst.get(str(r.instrument_id))
        cand = {
            "symbol": r.symbol,
            "direction": _direction_from_oi(r.oi_structure),
            "strategy": (prop.strategy_name if prop else (r.oi_structure or "tbd")),
            "composite_score": r.composite_score,
            "iv_regime": r.iv_regime or "n/a",
            "thesis": r.llm_thesis or "",
        }
        if prop:
            cand["contract"] = prop.short_label()
            cand["underlying_ltp"] = prop.underlying_ltp
            cand["target_premium"] = prop.target_premium
            cand["stop_premium"] = prop.stop_premium
        candidates.append(cand)

    msg = format_morning_brief(run_date=run_date.isoformat(), candidates=candidates)

    # Append a digest of currently-open paper positions (carried over from
    # prior sessions or freshly opened earlier today). Adds MTM context so
    # the operator sees the full book at the same time as new candidates.
    try:
        from src.fno.position_manager import (
            format_position_digest,
            open_positions_summary,
        )
        positions = await open_positions_summary()
        if positions:
            msg = msg + "\n\n" + format_position_digest(positions)
    except Exception as exc:
        logger.warning(f"morning brief: position digest failed: {exc}")

    await get_gateway().send_telegram(msg, parse_mode="MarkdownV2")

    async with session_scope() as session:
        session.add(
            Notification(
                type="system",
                priority="medium",
                title=f"F&O Morning Brief ({run_date.isoformat()})",
                body=msg,
                is_pushed=True,
                pushed_at=_dt.now(tz=timezone.utc),
                push_channel="telegram",
            )
        )
    logger.info(f"fno.orchestrator: morning brief sent — {len(candidates)} candidate(s)")


def _direction_from_oi(oi_structure: str | None) -> str:
    """Map oi_structure / strategy hint to a coarse bullish/bearish/neutral tag."""
    if not oi_structure:
        return "neutral"
    s = oi_structure.lower()
    if "bull" in s or "long" in s:
        return "bullish"
    if "bear" in s or "short" in s:
        return "bearish"
    return "neutral"
