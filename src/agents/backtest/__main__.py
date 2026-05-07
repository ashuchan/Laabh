"""CLI entry point: `python -m src.agents.backtest --date YYYY-MM-DD`.

Runs one workflow against the snapshot at `as_of` morning and writes a
markdown report under `reports/` (or `--out`). Default mode is mock-LLM,
read-only — safe to run any time without burning API budget or polluting
the DB.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date, datetime, time
from pathlib import Path

from src.agents.backtest.runner import BacktestRunner
from src.agents.backtest.report import render_backtest_report
from src.agents.backtest.snapshot import IST


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run a dry-run backtest of an agentic workflow.",
    )
    p.add_argument("--date", required=False,
                   help="Target date in YYYY-MM-DD (default: today, IST).")
    p.add_argument("--workflow", default="predict_today_combined",
                   help="Registered workflow name (default: predict_today_combined).")
    p.add_argument("--live-llm", action="store_true",
                   help="Use the real Anthropic API instead of the mock client.")
    p.add_argument("--persist-to-db", action="store_true",
                   help="Commit workflow_runs/agent_runs/agent_predictions rows. "
                        "Off by default — backtests are read-only.")
    p.add_argument("--force-proceed", action="store_true",
                   help="Override brain_triage skip_today decisions and synthesise "
                        "candidates from top signal symbols, so the rest of the "
                        "pipeline runs even on quiet/high-VIX days.")
    p.add_argument("--morning-verdict-from", type=str, default=None,
                   help="Path to a JSON dump of a previous morning workflow run. "
                        "Used by midday_review to seed midday_ceo with today's "
                        "morning allocation. Looks for stage_outputs.judge_verdict.")
    p.add_argument("--watch-symbols", type=str, default=None,
                   help="Comma-separated list of symbols for the midday review's "
                        "intraday collectors. Defaults to top signal symbols.")
    p.add_argument("--universe-size", type=int, default=30,
                   help="How many universe rows to seed into the snapshot (default 30).")
    p.add_argument("--out", default="reports",
                   help="Directory for the markdown report (default: reports/).")
    p.add_argument("--json", action="store_true",
                   help="Also write a JSON dump of the BacktestResult.")
    p.add_argument("--transcript", action="store_true",
                   help="Write a JSONL transcript with every LLM call's full prompt and response. "
                        "One line per call.")
    p.add_argument("--full-prompts", action="store_true",
                   help="Disable prompt/response truncation in the markdown report. "
                        "The file will be large — useful for prompt-engineering review.")
    p.add_argument("--quiet", action="store_true",
                   help="Suppress console summary; just print the report path.")
    return p.parse_args()


async def _run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    target_date = (
        date.fromisoformat(args.date)
        if args.date
        else datetime.now(IST).date()
    )
    as_of = datetime.combine(target_date, time(9, 0), tzinfo=IST)

    runner = BacktestRunner(
        db_session_factory=_get_db_factory(),
        mock_llm=not args.live_llm,
        persist_to_db=args.persist_to_db,
        force_proceed=args.force_proceed,
    )

    morning_verdict = None
    if args.morning_verdict_from:
        with open(args.morning_verdict_from, encoding="utf-8") as f:
            mvf = json.load(f)
        morning_verdict = (mvf.get("stage_outputs") or {}).get("judge_verdict") or mvf

    watch_symbols = None
    if args.watch_symbols:
        watch_symbols = [s.strip() for s in args.watch_symbols.split(",") if s.strip()]

    result = await runner.run(
        args.workflow,
        as_of=as_of,
        universe_size=args.universe_size,
        morning_verdict=morning_verdict,
        watch_symbols=watch_symbols,
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "live" if args.live_llm else "mock"
    md_path = out_dir / f"backtest-{args.workflow}-{target_date}-{suffix}.md"
    md_path.write_text(
        render_backtest_report(result, full_prompts=args.full_prompts),
        encoding="utf-8",
    )

    if args.json:
        json_path = md_path.with_suffix(".json")
        json_path.write_text(_to_json(result), encoding="utf-8")

    if args.transcript:
        from src.agents.backtest.transcript import write_transcript
        transcript_path = md_path.with_suffix(".transcript.jsonl")
        write_transcript(result, transcript_path)
        print(f"Transcript:   {transcript_path}")

    if not args.quiet:
        _print_console_summary(result, md_path)
    else:
        print(md_path)

    # Exit code: 0 success, 10 succeeded_with_caveats, 20 failed.
    if result.status == "failed":
        return 20
    if (result.status_extended or "") == "succeeded_with_caveats":
        return 10
    return 0


def _get_db_factory():
    """Try to use the live DB; fall back to a stub if unavailable."""
    try:
        from src.db import get_session_factory
        return get_session_factory()
    except Exception as e:
        logging.warning("Falling back to stub DB factory: %s", e)
        from src.agents.backtest.runner import _make_stub_factory
        return _make_stub_factory()


def _to_json(result) -> str:
    """Serialise BacktestResult to JSON."""
    return json.dumps(
        {
            "workflow_name": result.workflow_name,
            "target_date": result.target_date.isoformat(),
            "as_of": result.as_of.isoformat(),
            "mock_llm": result.mock_llm,
            "persist_to_db": result.persist_to_db,
            "workflow_run_id": result.workflow_run_id,
            "status": result.status,
            "status_extended": result.status_extended,
            "error": result.error,
            "short_circuit_reason": result.short_circuit_reason,
            "actual_cost_usd": str(result.actual_cost_usd),
            "projected_cost_usd": str(result.projected_cost_usd),
            "total_tokens": result.total_tokens,
            "api_calls": result.api_calls,
            "agent_runs": result.agent_runs,
            "predictions": result.predictions,
            "validator_outcomes": result.validator_outcomes,
            "stage_outputs": result.stage_outputs,
            "pnl_estimates": result.pnl_estimates,
            "aggregate_pnl_pct": result.aggregate_pnl_pct,
        },
        default=str,
        indent=2,
    )


def _print_console_summary(r, md_path: Path) -> None:
    status = r.status_extended or r.status
    # ASCII glyphs only — Windows cp1252 console can't render emoji.
    emoji = {
        "succeeded": "[OK]",
        "succeeded_with_caveats": "[WARN]",
        "failed": "[FAIL]",
        "cancelled": "[CANCEL]",
    }.get(status, "[?]")
    print()
    print(f"=== Backtest: {r.workflow_name} for {r.target_date} ===")
    print(f"Status:       {emoji} {status}")
    print(f"API calls:    {r.api_calls} (mode: {'mock' if r.mock_llm else 'live'})")
    print(f"Cost:         ${float(r.actual_cost_usd):.4f}  "
          f"(projected ceiling ${float(r.projected_cost_usd):.4f})")
    print(f"Tokens:       {r.total_tokens:,}")
    print(f"Predictions:  {len(r.predictions)}")
    if r.aggregate_pnl_pct is not None:
        print(f"Sim P&L:      {r.aggregate_pnl_pct:+.2f}% "
              f"(close-to-close, deployed capital only)")
    if r.error:
        print(f"Error:        {r.error}")
    print(f"Report:       {md_path}")
    print()


def main() -> None:
    args = _parse_args()
    try:
        sys.exit(asyncio.run(_run(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
