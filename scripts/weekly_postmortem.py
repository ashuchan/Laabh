#!/usr/bin/env python3
"""Weekly postmortem runner — intended to run every Sunday at 18:00 IST.

Cron (IST = UTC+5:30):
    30 12 * * 0  cd /path/to/laabh && python scripts/weekly_postmortem.py

Usage:
    python scripts/weekly_postmortem.py               # last complete week
    python scripts/weekly_postmortem.py --week 2026-W18
    python scripts/weekly_postmortem.py --week 2026-W18 --no-ab
    python scripts/weekly_postmortem.py --week 2026-W18 --ab-override fno_expert=v2
    python scripts/weekly_postmortem.py --week 2026-W18 --dry-run

Outputs:
    reports/weekly/<YYYY-Www>.md  — markdown report
    Telegram digest message (if configured)
    DB rows in prompt_version_results (if A/B results present)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("weekly_postmortem")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate the Laabh weekly postmortem.")
    p.add_argument(
        "--week",
        default=None,
        metavar="YYYY-Www",
        help="ISO week string (e.g. 2026-W18). Defaults to last completed week.",
    )
    p.add_argument(
        "--no-replay-ab",
        dest="no_ab",
        action="store_true",
        help="Skip A/B prompt-version replay.",
    )
    p.add_argument(
        "--ab-versions",
        dest="ab_override",
        default=[],
        action="append",
        metavar="AGENT=VERSION",
        help="Candidate prompt version for A/B replay (repeatable).",
    )
    p.add_argument(
        "--no-regression",
        action="store_true",
        help="Skip regression suite.",
    )
    p.add_argument(
        "--no-telegram",
        action="store_true",
        help="Suppress Telegram digest.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch data and log report without writing files or sending messages.",
    )
    return p.parse_args()


def _resolve_week(week_str: str | None) -> tuple[date, date, str]:
    """Return (start, end, iso_week_label)."""
    if week_str:
        parsed = date.fromisoformat(f"{week_str}-1")  # ISO week Monday
        start = parsed
    else:
        today = date.today()
        # last Monday
        start = today - timedelta(days=today.weekday() + 7)
    end = start + timedelta(days=6)
    iso_label = start.strftime("%G-W%V")
    return start, end, iso_label


async def _run(args: argparse.Namespace) -> None:
    from src.config import settings
    from src.db import get_async_session
    from src.eval.weekly import (
        fetch_week_data,
        compute_pnl_attribution,
        compute_calibration_drift,
        compute_cost_per_correct_prediction,
        render_markdown_report,
        send_telegram_digest,
        persist_prompt_version_results,
    )
    from src.eval.regression import run_regression_suite
    from src.eval.ab import run_prompt_version_ab

    start, end, week_iso = _resolve_week(args.week)
    log.info("Weekly postmortem for %s (%s to %s)", week_iso, start, end)

    # --- fetch data ---
    log.info("Fetching week data from DB…")
    week = await fetch_week_data(start, end, get_async_session)
    log.info(
        "Fetched: %d workflow runs, %d agent runs, %d resolved predictions, %d eval scores",
        len(week.workflow_runs),
        len(week.agent_runs),
        len(week.resolved_predictions),
        len(week.shadow_eval_scores),
    )

    if not week.workflow_runs:
        log.warning("No workflow runs found for %s — aborting.", week_iso)
        return

    # --- analytics ---
    pnl_attribution = compute_pnl_attribution(week)
    calibration_drift = compute_calibration_drift(week)
    cost_correct = compute_cost_per_correct_prediction(week)

    log.info(
        "P&L: %+.1f%% | Win rate: %.0f%% | LLM spend: $%.2f",
        pnl_attribution["week_total_pnl_pct"],
        cost_correct["win_rate_pct"],
        cost_correct["total_llm_cost_usd"],
    )

    # --- regression suite ---
    regression_results: list[dict] = []
    if not args.no_regression:
        log.info("Running regression suite…")
        try:
            from src.agents.runtime import WorkflowRunner
            import anthropic
            anthropic_client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
            runner = WorkflowRunner(
                db_session_factory=get_async_session,
                redis=None,
                anthropic=anthropic_client,
                telegram=None,
            )
            regression_results = await run_regression_suite(runner)
            n_pass = sum(1 for r in regression_results if r["passed"])
            log.info("Regression: %d/%d seeds passed", n_pass, len(regression_results))
        except Exception as e:
            log.error("Regression suite failed: %s", e)
    else:
        log.info("Regression suite skipped (--no-ab).")

    # --- A/B prompt versioning ---
    ab_results: list[dict] = []
    if not args.no_ab and args.ab_override:
        log.info("Running A/B replay for: %s", args.ab_override)
        try:
            from src.agents.runtime import WorkflowRunner
            import anthropic
            anthropic_client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
            runner = WorkflowRunner(
                db_session_factory=get_async_session,
                redis=None,
                anthropic=anthropic_client,
                telegram=None,
            )
            ab_results = await run_prompt_version_ab(
                runner=runner,
                week_data=week,
                candidate_versions=args.ab_override,
            )
            log.info("A/B results: %d version(s) tested", len(ab_results))
        except Exception as e:
            log.error("A/B replay failed: %s", e)
    elif not args.ab_override:
        log.info("No --ab-override specified; skipping A/B replay.")

    # --- render markdown ---
    report_md = render_markdown_report(
        week_iso=week_iso,
        week=week,
        pnl_attribution=pnl_attribution,
        calibration_drift=calibration_drift,
        cost_correct=cost_correct,
        regression_results=regression_results,
        ab_results=ab_results,
    )

    if args.dry_run:
        log.info("DRY-RUN — not writing files or sending messages.")
        print("\n" + "=" * 60)
        print(report_md)
        print("=" * 60 + "\n")
        return

    # --- write report ---
    reports_dir = Path(__file__).parent.parent / "reports" / "weekly"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"{week_iso}.md"
    report_path.write_text(report_md, encoding="utf-8")
    log.info("Report written to %s", report_path)

    # --- persist A/B results ---
    if ab_results:
        try:
            await persist_prompt_version_results(ab_results, week_iso, get_async_session)
            log.info("A/B results persisted to prompt_version_results.")
        except Exception as e:
            log.error("Failed to persist A/B results: %s", e)

    # --- Telegram digest ---
    if not args.no_telegram:
        try:
            from src.services.notification_service import TelegramNotifier
            telegram = TelegramNotifier(settings.TELEGRAM_BOT_TOKEN)
            await send_telegram_digest(
                week_iso=week_iso,
                week=week,
                pnl_attribution=pnl_attribution,
                calibration_drift=calibration_drift,
                telegram=telegram,
                chat_id=settings.TELEGRAM_CHAT_ID,
            )
            log.info("Telegram digest sent.")
        except Exception as e:
            log.warning("Telegram digest failed: %s", e)

    log.info("Weekly postmortem complete for %s.", week_iso)


def main() -> None:
    args = _parse_args()
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        log.info("Interrupted.")
        sys.exit(0)
    except Exception as e:
        log.error("Postmortem failed: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
