"""Run a backtest range, then generate the analysis report.

Thin wrapper around ``scripts.backtest_run`` and ``scripts.backtest_report``
so the dashboard's "Run + generate report" button has a single, clean
entry point instead of synthesizing a multi-step shell command at run-time.

Usage:
    python -m scripts.backtest_run_and_report \\
        --start-date 2026-05-04 --end-date 2026-05-08 \\
        --portfolio-id <uuid> [--seed 42] [--risk-free-rate 0.0525] \\
        [--smile-method linear]

Exit code is the run script's exit code if non-zero, else the report
script's exit code.
"""
from __future__ import annotations

import argparse
import subprocess
import sys


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="backtest_run_and_report")
    p.add_argument("--start-date", required=True)
    p.add_argument("--end-date", required=True)
    p.add_argument("--portfolio-id", required=True)
    p.add_argument("--seed", default="42")
    p.add_argument("--risk-free-rate", default=None)
    p.add_argument("--smile-method", default=None)
    p.add_argument("--out", default=None, help="Override report output path")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    run_argv: list[str] = [
        sys.executable, "-m", "scripts.backtest_run",
        "--start-date", args.start_date,
        "--end-date", args.end_date,
        "--portfolio-id", args.portfolio_id,
        "--seed", str(args.seed),
    ]
    if args.risk_free_rate is not None:
        run_argv += ["--risk-free-rate", str(args.risk_free_rate)]
    if args.smile_method is not None:
        run_argv += ["--smile-method", args.smile_method]

    print(f"=> {' '.join(run_argv)}", flush=True)
    rc = subprocess.run(run_argv).returncode
    if rc != 0:
        # Partial failure (e.g. one of N days threw): keep going. The
        # report script handles the case where some days have no rows.
        # The user gets visibility into the days that *did* succeed.
        print(
            f"backtest_run exited with code {rc} (partial failure). "
            f"Generating report on whatever days completed.",
            file=sys.stderr,
            flush=True,
        )

    report_argv = [
        sys.executable, "-m", "scripts.backtest_report",
        "--start-date", args.start_date,
        "--end-date", args.end_date,
        "--portfolio-id", args.portfolio_id,
    ]
    if args.out:
        report_argv += ["--out", args.out]
    print(f"=> {' '.join(report_argv)}", flush=True)
    report_rc = subprocess.run(report_argv).returncode

    # Surface either failure: a non-zero run rc takes precedence so CI/CLI
    # callers can detect the broken day, but the report still got written.
    return rc if rc != 0 else report_rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
