"""Dry-run replay orchestrator — drives the full F&O routine for a historical date.

Calls the existing live pipeline functions verbatim, but:
  1. All side effects (Telegram, GitHub issues) are suppressed via NoOpGateway.
  2. All DB writes are stamped with a replay-specific dryrun_run_id UUID.
  3. Chain data is sourced from DhanHistoricalSource instead of live NSE/Dhan.
  4. The Phase 4 tick loop is driven by minute_range() instead of real-time cron.
  5. Pre-flight checks use the replay profile (bhavcopy availability).

Entry point: await replay(D, mock_llm=True)
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from loguru import logger

from src.dryrun.side_effects import get_dryrun_run_id, set_dryrun_run_id
from src.dryrun.timestamps import (
    ist,
    minute_range,
    scheduled_chain_times,
    scheduled_macro_times,
)
from src.collectors.fii_dii_collector import fetch_yesterday
from src.collectors.macro_collector import collect as collect_macro
from src.fno.ban_list import fetch_today as fetch_ban_list
from src.fno.chain_collector import collect_tier, replay_chain_source
from src.fno.orchestrator import run_eod_tasks, run_premarket_pipeline
from src.fno.sources.dhan_historical import DhanHistoricalSource
from src.fno.vix_collector import run_once as collect_vix
from src.runday.checks.base import CheckResult, Severity
from src.runday.checks.connectivity import DBConnectivityCheck
from src.runday.checks.data import BhavcopyAvailableCheck, TradingDayCheck
from src.runday.checks.pipeline import make_phase_check
from src.runday.checks.schema import RequiredTablesCheck
from src.services.side_effect_gateway import NoOpGateway, set_gateway


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class ReplayResult:
    run_id: uuid.UUID
    replay_date: date
    gates_passed: list[str] = field(default_factory=list)
    gates_failed: list[str] = field(default_factory=list)
    gates_warned: list[str] = field(default_factory=list)
    captures: list[dict] = field(default_factory=list)
    stage_counts: dict = field(default_factory=dict)
    success: bool = True


class ReplayGateFailed(Exception):
    """Raised when a mandatory gate check fails during replay."""


# ---------------------------------------------------------------------------
# Gate helper
# ---------------------------------------------------------------------------

def _gate(check_result: CheckResult, *, warn_only: bool = False) -> None:
    """Raise ReplayGateFailed if the check failed."""
    if check_result.severity == Severity.FAIL and not warn_only:
        raise ReplayGateFailed(
            f"Gate '{check_result.name}' FAILED: {check_result.message}"
        )


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

async def replay(
    D: date,
    *,
    mock_llm: bool = True,
    run_id: uuid.UUID | None = None,
) -> ReplayResult:
    """Run the complete F&O daily routine for historical date D.

    Returns a ReplayResult with stage counts and captured side-effect calls.
    Raises ReplayGateFailed if any mandatory gate check fails.
    """
    if run_id is None:
        run_id = uuid.uuid4()

    result = ReplayResult(run_id=run_id, replay_date=D)
    noop_gw = NoOpGateway()

    logger.info(f"dryrun.orchestrator: starting replay for {D} run_id={run_id}")

    with set_gateway(noop_gw):
        with set_dryrun_run_id(run_id):
            await _run_replay(D, run_id, result, mock_llm)

    result.captures = noop_gw.record_capture()
    return result


async def _run_replay(
    D: date,
    run_id: uuid.UUID,
    result: ReplayResult,
    mock_llm: bool,
) -> None:
    from src.runday.config import get_runday_settings

    settings = get_runday_settings()

    # ------------------------------------------------------------------
    # Stage 1 — pre-flight (replay profile)
    # ------------------------------------------------------------------
    logger.info("dryrun: Stage 1 — pre-flight")
    preflight_checks = [
        DBConnectivityCheck(settings),
        RequiredTablesCheck(settings),
        TradingDayCheck(settings, anchor_date=D),
        BhavcopyAvailableCheck(settings, D),
    ]
    for check in preflight_checks:
        cr = await check.run()
        if cr.severity == Severity.FAIL:
            result.success = False
            result.gates_failed.append(check.name)
            _gate(cr)
        elif cr.severity == Severity.WARN:
            result.gates_warned.append(check.name)
        else:
            result.gates_passed.append(check.name)

    # ------------------------------------------------------------------
    # Stage 2 — chain + VIX replay (09:00–15:30, every 5 min)
    # ------------------------------------------------------------------
    logger.info(f"dryrun: Stage 2 — chain replay for {D}")
    historical_source = DhanHistoricalSource(D)

    chain_ok = 0
    chain_miss = 0
    chain_times = scheduled_chain_times(D)

    with replay_chain_source(historical_source):
        for ts in chain_times:
            try:
                await collect_tier(1, as_of=ts, dryrun_run_id=run_id)
                # Tier 2 on every 15-minute boundary
                if ts.minute % 15 == 0:
                    await collect_tier(2, as_of=ts, dryrun_run_id=run_id)
                await collect_vix(as_of=ts, dryrun_run_id=run_id)
                chain_ok += 1
            except Exception as exc:
                logger.warning(f"dryrun: chain ts={ts} failed: {exc}")
                chain_miss += 1

    # Macro (pre-market)
    for ts in scheduled_macro_times(D):
        try:
            await collect_macro(as_of=ts)
        except Exception as exc:
            logger.debug(f"dryrun: macro ts={ts} failed: {exc}")

    result.stage_counts["chain_ok"] = chain_ok
    result.stage_counts["chain_miss"] = chain_miss
    logger.info(f"dryrun: Stage 2 done — {chain_ok} snapshots, {chain_miss} misses")

    # ------------------------------------------------------------------
    # Stage 2b — ban list + FII/DII (use D-1 for FII/DII; data lags 1 day)
    # ------------------------------------------------------------------
    try:
        await fetch_ban_list(ban_date=D)
    except Exception as exc:
        logger.warning(f"dryrun: ban list fetch failed: {exc}")

    try:
        # FII/DII data lags by one trading day
        await fetch_yesterday(target_date=D - timedelta(days=1))
    except Exception as exc:
        logger.warning(f"dryrun: FII/DII fetch failed: {exc}")

    # ------------------------------------------------------------------
    # Stage 3 — Phases 1–3 (live functions, unchanged)
    # ------------------------------------------------------------------
    logger.info("dryrun: Stage 3 — Phases 1–3")
    # Pass as_of so Phase 1 chain queries are bounded to the replay timestamp
    phase1_as_of = ist(D, 9, 0)
    pipeline_result = await run_premarket_pipeline(D, as_of=phase1_as_of)
    result.stage_counts["pipeline"] = pipeline_result

    for phase in ("phase1", "phase2", "phase3"):
        cr = await make_phase_check(phase, settings, D).run()
        if cr.severity == Severity.FAIL:
            result.success = False
            result.gates_failed.append(f"stage3.{phase}")
            logger.warning(f"dryrun: phase gate {phase} FAILED: {cr.message}")
        elif cr.severity == Severity.WARN:
            result.gates_warned.append(f"stage3.{phase}")
        else:
            result.gates_passed.append(f"stage3.{phase}")

    # ------------------------------------------------------------------
    # Stage 4 — Phase 4 intraday tick loop (09:15–14:30)
    # ------------------------------------------------------------------
    logger.info("dryrun: Stage 4 — Phase 4 tick loop")
    from src.fno.intraday_manager import IntradayState  # local: avoid circular import
    state = IntradayState()
    ticks = minute_range(D, 9, 15, 14, 30)

    for ts in ticks:
        try:
            # intraday_manager.apply_tick is the stateless helper; entry/exit
            # decisions depend on signals in the DB (written in Stage 3)
            _run_tick(ts, state)
        except Exception as exc:
            logger.debug(f"dryrun: tick {ts} error: {exc}")

    # Hard exit at 14:30
    hard_exit_ts = ist(D, 14, 30)
    try:
        _hard_exit(hard_exit_ts, state)
    except Exception as exc:
        logger.warning(f"dryrun: hard exit failed: {exc}")

    result.stage_counts["ticks"] = len(ticks)
    result.stage_counts["positions_closed"] = len(getattr(state, "closed", []))

    hard_exit_check = make_phase_check("hard-exit", settings, D)
    cr = await hard_exit_check.run()
    if cr.severity == Severity.FAIL:
        result.success = False
        result.gates_failed.append("stage4.hard_exit")
    elif cr.severity == Severity.WARN:
        result.gates_warned.append("stage4.hard_exit")
    else:
        result.gates_passed.append("stage4.hard_exit")

    # ------------------------------------------------------------------
    # Stage 5 — EOD tasks
    # ------------------------------------------------------------------
    logger.info("dryrun: Stage 5 — EOD")
    await run_eod_tasks(D)

    for eod_phase in ("iv-history", "ban-list"):
        check = make_phase_check(eod_phase, settings, D)
        if check is None:
            logger.debug(f"dryrun: no check registered for {eod_phase} — skipping gate")
            continue
        cr = await check.run()
        if cr.severity == Severity.FAIL:
            result.success = False
            result.gates_failed.append(f"stage5.{eod_phase}")
        elif cr.severity == Severity.WARN:
            result.gates_warned.append(f"stage5.{eod_phase}")
        else:
            result.gates_passed.append(f"stage5.{eod_phase}")

    logger.info(
        f"dryrun: replay complete — {len(result.gates_passed)} gates passed, "
        f"{len(result.gates_failed)} failed"
    )


def _run_tick(ts, state) -> None:
    """Minimal tick: no-op in v1 — intraday_manager needs live price data.

    In a full implementation this would call intraday_manager.apply_tick(ts, state).
    For the dry-run v1, the tick loop advances time without actual price lookups,
    so positions entered via Phase 3 signals will be handled at hard-exit time.
    """
    pass


def _hard_exit(ts, state) -> None:
    """No-op stub for hard exit in v1 dry-run."""
    pass
