"""laabh-runday — live-day operations CLI."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Optional

import pytz
import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.live import Live

# Load .env from the repo root so EnvCheck and other os.environ readers see
# values the user keeps in their .env file. override=False respects vars
# already set in the shell.
load_dotenv(override=False)

from src.runday.checks.audit import LLMAuditCheck
from src.runday.checks.chain import ChainCollectionHealthCheck, OpenIssuesCheck, SourceHealthCheck, get_tier_breakdown
from src.runday.checks.connectivity import (
    AngelOneCheck,
    AnthropicCheck,
    DBConnectivityCheck,
    DhanCheck,
    EnvCheck,
    GitHubCheck,
    NSECheck,
    TelegramCheck,
)
from src.runday.checks.data import BanListCheck, BhavcopyAvailableCheck, IVHistoryCoverageCheck, TierTableCheck, TradingDayCheck
from src.runday.checks.pipeline import make_phase_check
from src.runday.checks.schema import MigrationsCurrentCheck, RequiredTablesCheck, SeedDataCheck
from src.runday.checks.trading import RiskCapCheck, TradingStatusCheck
from src.runday.checks.base import CheckResult, Severity, exit_code_for
from src.runday.config import get_runday_settings
from src.runday.reporters import console as console_reporter
from src.runday.reporters import json_out
from src.runday.reporters.telegram import TelegramReporter

_IST = pytz.timezone("Asia/Kolkata")
_console = Console()

app = typer.Typer(
    name="laabh-runday",
    help="Laabh live-day operations routine — preflight, checkpoint, status, report.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

_VALID_PHASES = [
    "tier-refresh", "phase1", "phase2", "phase3",
    "morning-brief", "phase4-entry", "phase4-manage",
    "hard-exit", "iv-history", "ban-list", "review-loop",
]


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------

@app.command()
def preflight(
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Skip Telegram message")] = False,
    json: Annotated[bool, typer.Option("--json", help="Emit JSON instead of console output")] = False,
    skip: Annotated[Optional[list[str]], typer.Option("--skip", help="Skip a specific check (repeatable)")] = None,
    profile: Annotated[str, typer.Option("--profile", help="Check profile: 'live' (default) or 'replay'")] = "live",
    date_str: Annotated[Optional[str], typer.Option("--date", help="Target date for replay profile (YYYY-MM-DD)")] = None,
) -> None:
    """Pre-market sanity check. Run the night before or by 6 AM on trade day.

    Exits 0 (all green), 10 (warnings), or 20 (any failure).

    Use --profile replay --date YYYY-MM-DD to check replay prerequisites.
    """
    target_date: date | None = None
    if date_str:
        try:
            target_date = date.fromisoformat(date_str)
        except ValueError:
            _console.print(f"[red]Invalid --date '{date_str}'. Use YYYY-MM-DD format.[/red]")
            raise SystemExit(1)

    if profile not in ("live", "replay"):
        _console.print(f"[red]Invalid --profile '{profile}'. Use 'live' or 'replay'.[/red]")
        raise SystemExit(1)

    code = asyncio.run(
        _preflight_async(
            quiet=quiet,
            emit_json=json,
            skip=set(skip or []),
            profile=profile,
            target_date=target_date,
        )
    )
    raise SystemExit(code)


async def _preflight_async(
    quiet: bool,
    emit_json: bool,
    skip: set[str],
    profile: str = "live",
    target_date: date | None = None,
) -> int:
    settings = get_runday_settings()
    telegram = TelegramReporter(settings)

    # Normalize skip names: accept either "dhan" or "preflight.dhan".
    skip = {s if s.startswith("preflight.") else f"preflight.{s}" for s in skip}

    if profile == "replay":
        if target_date is None:
            target_date = date.today()
        checks = [
            DBConnectivityCheck(settings),
            MigrationsCurrentCheck(settings),
            RequiredTablesCheck(settings),
            AnthropicCheck(settings),
            TradingDayCheck(settings, anchor_date=target_date),
            BhavcopyAvailableCheck(settings, target_date),
        ]
    else:
        checks = [
            EnvCheck(settings, skipped_checks=skip),
            DBConnectivityCheck(settings),
            MigrationsCurrentCheck(settings),
            RequiredTablesCheck(settings),
            SeedDataCheck(settings),
            AnthropicCheck(settings),
            TelegramCheck(settings, quiet=quiet),
            AngelOneCheck(settings),
            NSECheck(settings),
            DhanCheck(settings),
            GitHubCheck(settings),
            TierTableCheck(settings),
            TradingDayCheck(settings),
        ]

    results: list[CheckResult] = []
    for check in checks:
        if check.name in skip:
            results.append(
                CheckResult(name=check.name, severity=Severity.OK, message="[skipped]")
            )
            continue
        result = await check.run()
        results.append(result)
        # Stop after first FAIL for env/db to avoid noisy cascades
        if result.severity == Severity.FAIL and check.name in (
            "preflight.env",
            "preflight.db_connectivity",
        ):
            # Fill remaining checks as skipped
            checked_names = {r.name for r in results}
            for remaining in checks:
                if remaining.name not in checked_names:
                    results.append(
                        CheckResult(
                            name=remaining.name,
                            severity=Severity.WARN,
                            message="[skipped — earlier critical check failed]",
                        )
                    )
            break

    if emit_json:
        print(json_out.emit_results(results))
    else:
        title = f"laabh-runday preflight ({profile})"
        console_reporter.render_check_list(results, title=title)
        console_reporter.render_summary_line(results)

    code = exit_code_for(results)

    # Only send Telegram for live preflight failures (replay is local-only)
    if profile == "live":
        any_fail = any(r.severity == Severity.FAIL for r in results)
        if any_fail:
            await telegram.send_preflight_fail(results)
        elif settings.runday_telegram_on_preflight_ok and not quiet:
            await telegram.send_preflight_ok(results)

    return code


# ---------------------------------------------------------------------------
# checkpoint
# ---------------------------------------------------------------------------

@app.command()
def checkpoint(
    phase: Annotated[str, typer.Argument(help=f"Phase to verify. One of: {', '.join(_VALID_PHASES)}")],
    json: Annotated[bool, typer.Option("--json", help="Emit JSON output")] = False,
    since: Annotated[Optional[str], typer.Option("--since", help="Override anchor date (ISO: YYYY-MM-DD)")] = None,
    strict: Annotated[bool, typer.Option("--strict", help="Treat warnings as failures")] = False,
) -> None:
    """Phase-specific verification after each expected completion time."""
    if phase not in _VALID_PHASES:
        _console.print(
            f"[red]Unknown phase '{phase}'. Valid phases: {', '.join(_VALID_PHASES)}[/red]"
        )
        raise SystemExit(1)

    anchor: date | None = None
    if since:
        try:
            anchor = date.fromisoformat(since)
        except ValueError:
            _console.print(f"[red]Invalid --since date '{since}'. Use YYYY-MM-DD format.[/red]")
            raise SystemExit(1)

    code = asyncio.run(_checkpoint_async(phase=phase, anchor=anchor, emit_json=json, strict=strict))
    raise SystemExit(code)


async def _checkpoint_async(
    phase: str,
    anchor: date | None,
    emit_json: bool,
    strict: bool,
) -> int:
    settings = get_runday_settings()

    # Map phase name to appropriate check(s)
    phase_to_data_check = {
        "iv-history": lambda: IVHistoryCoverageCheck(settings, anchor),
        "ban-list": lambda: BanListCheck(settings, anchor),
    }

    check = phase_to_data_check.get(phase, lambda: make_phase_check(phase, settings, anchor))()

    if check is None:
        _console.print(f"[red]No check implemented for phase '{phase}'[/red]")
        raise SystemExit(1)

    result = await check.run()
    results = [result]

    if emit_json:
        print(json_out.emit_results(results))
    else:
        console_reporter.render_check_list(results, title=f"checkpoint: {phase}")
        console_reporter.render_summary_line(results)

    if strict:
        return 0 if result.severity == Severity.OK else 20
    return exit_code_for(results)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@app.command()
def status(
    json: Annotated[bool, typer.Option("--json", help="Dashboard as JSON")] = False,
    once: Annotated[bool, typer.Option("--once", help="Run one snapshot and exit (default)")] = True,
    watch: Annotated[bool, typer.Option("--watch", help="Refresh every 60s using rich.live")] = False,
) -> None:
    """Live snapshot of the current pipeline state. Fast (<2s)."""
    asyncio.run(_status_async(emit_json=json, watch=watch))


async def _status_async(emit_json: bool, watch: bool) -> None:
    settings = get_runday_settings()

    async def _snapshot() -> dict:
        return await _collect_status_data(settings)

    if watch:
        import time as _time
        with Live(refresh_per_second=1, screen=True) as live:
            while True:
                data = await _snapshot()
                if emit_json:
                    live.update(json_out.emit_status(data))
                else:
                    from io import StringIO
                    from rich.console import Console as _C
                    buf = StringIO()
                    tmp = _C(file=buf, highlight=False)
                    # Re-render to the live buffer — use text capture
                    live.update(_build_status_renderable(data))
                _time.sleep(60)
    else:
        data = await _snapshot()
        if emit_json:
            print(json_out.emit_status(data))
        else:
            console_reporter.render_status_dashboard(data)


async def _collect_status_data(settings) -> dict:
    """Query all status data in parallel."""
    import asyncio as _asyncio

    chain_check = ChainCollectionHealthCheck(settings, lookback_minutes=10)
    source_check = SourceHealthCheck(settings)
    issues_check = OpenIssuesCheck(settings)
    trading_check = TradingStatusCheck(settings)

    chain_r, source_r, issues_r, trading_r = await _asyncio.gather(
        chain_check.run(),
        source_check.run(),
        issues_check.run(),
        trading_check.run(),
        return_exceptions=True,
    )

    def _safe(r) -> dict:
        return r.details if isinstance(r, CheckResult) else {}

    chain_data = _safe(chain_r)
    source_data = _safe(source_r)
    issues_data = _safe(issues_r)
    trading_data = _safe(trading_r)

    # Latest VIX
    vix_data: dict = {}
    try:
        from sqlalchemy import text
        from src.db import session_scope
        async with session_scope() as session:
            vr = await session.execute(
                text("SELECT vix_value, regime FROM vix_ticks ORDER BY timestamp DESC LIMIT 1")
            )
            vix_row = vr.fetchone()
            if vix_row:
                vix_data = {"value": float(vix_row[0]), "regime": vix_row[1]}
    except Exception:
        pass

    # Pipeline today
    pipeline_data: dict = {}
    try:
        today = date.today()
        from sqlalchemy import text
        from src.db import session_scope
        async with session_scope() as session:
            # Phase counts
            for phase_num in (1, 2, 3):
                pr = await session.execute(
                    text(
                        "SELECT COUNT(*) FROM fno_candidates "
                        "WHERE phase = :p AND run_date = :d"
                    ),
                    {"p": phase_num, "d": today.isoformat()},
                )
                count = pr.scalar() or 0
                pipeline_data[f"phase{phase_num}"] = {
                    "ok": count > 0,
                    "value": f"{count} candidates" if phase_num < 3 else f"{count} theses",
                }

            # Morning brief
            mbr = await session.execute(
                text(
                    "SELECT MAX(pushed_at) FROM notifications "
                    "WHERE (type='system' OR title ILIKE '%morning%brief%') "
                    "AND DATE(created_at AT TIME ZONE 'UTC') = :d "
                    "AND is_pushed = true"
                ),
                {"d": today.isoformat()},
            )
            mb_time = mbr.scalar()
            pipeline_data["morning_brief"] = {
                "ok": mb_time is not None,
                "value": f"sent at {mb_time.strftime('%H:%M:%S')}" if mb_time else "not sent",
            }

            # Ban list count
            banr = await session.execute(
                text("SELECT COUNT(*) FROM fno_ban_list WHERE ban_date = :d"),
                {"d": today.isoformat()},
            )
            pipeline_data["ban_list_count"] = banr.scalar() or 0
            pipeline_data["vix"] = vix_data

    except Exception:
        pass

    # Recent jobs
    jobs_data: list[dict] = []
    try:
        from sqlalchemy import text
        from src.db import session_scope
        async with session_scope() as session:
            jr = await session.execute(
                text(
                    "SELECT job_name, MAX(created_at) as last_run FROM job_log "
                    "GROUP BY job_name ORDER BY last_run DESC LIMIT 8"
                )
            )
            for job_name, last_run in jr.fetchall():
                jobs_data.append({"name": job_name, "last_run": str(last_run)})
    except Exception:
        pass

    return {
        "chain": chain_data,
        "sources": source_data,
        "issues": issues_data,
        "trading": trading_data,
        "pipeline": pipeline_data,
        "next_jobs": jobs_data,
    }


def _build_status_renderable(data: dict):
    """Build a rich renderable from status data (for Live mode)."""
    from rich.text import Text
    from io import StringIO
    from rich.console import Console as _C
    buf = StringIO()
    tmp = _C(file=buf, no_color=False)
    # Temporarily swap the module-level console
    orig = console_reporter._CONSOLE
    console_reporter._CONSOLE = tmp
    console_reporter.render_status_dashboard(data)
    console_reporter._CONSOLE = orig
    return Text.from_ansi(buf.getvalue())


# ---------------------------------------------------------------------------
# tier-check
# ---------------------------------------------------------------------------

@app.command(name="tier-check")
def tier_check(
    filter_: Annotated[Optional[str], typer.Option("--filter", help="'degraded' to show <80% success only")] = None,
    tier: Annotated[Optional[int], typer.Option("--tier", help="Filter by tier (1 or 2)")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max rows to show")] = 50,
    json: Annotated[bool, typer.Option("--json", help="Emit JSON output")] = False,
) -> None:
    """Per-instrument chain coverage diagnostic. Useful to localize degradation."""
    asyncio.run(_tier_check_async(filter_=filter_, tier=tier, limit=limit, emit_json=json))


async def _tier_check_async(
    filter_: str | None,
    tier: int | None,
    limit: int,
    emit_json: bool,
) -> None:
    settings = get_runday_settings()
    only_degraded = filter_ == "degraded"
    rows = await get_tier_breakdown(
        settings,
        lookback_minutes=60,
        tier_filter=tier,
        only_degraded=only_degraded,
        limit=limit,
    )

    if emit_json:
        import json as _json
        print(_json.dumps({"rows": rows, "count": len(rows)}, indent=2))
        return

    from rich.table import Table
    table = Table(title="Chain Coverage by Instrument (last 60 min)", box=None)
    table.add_column("Symbol", style="bold")
    table.add_column("Tier", justify="center")
    table.add_column("Last Attempt")
    table.add_column("Last Status")
    table.add_column("Success% 1h", justify="right")
    table.add_column("Sources")

    for row in rows:
        rate = row.get("success_rate_1h")
        rate_str = f"{rate:.1f}%" if rate is not None else "n/a"
        color = "red" if (rate is not None and rate < 80) else "green"
        table.add_row(
            row["symbol"],
            str(row["tier"]),
            str(row.get("last_attempt", "")[:19] if row.get("last_attempt") else "never"),
            row.get("last_status", ""),
            f"[{color}]{rate_str}[/{color}]",
            str(row.get("source_breakdown", {})),
        )

    _console.print(table)
    _console.print(f"\n{len(rows)} instrument(s) shown")


# ---------------------------------------------------------------------------
# kill-switch
# ---------------------------------------------------------------------------

@app.command(name="kill-switch")
def kill_switch(
    reason: Annotated[Optional[str], typer.Option("--reason", help="Reason text appended to alert")] = None,
) -> None:
    """Arm the F&O kill-switch: sets FNO_MODULE_ENABLED=false in .env atomically.

    Does NOT kill the process — prints the PID and instructions for the operator.
    """
    asyncio.run(_kill_switch_async(reason=reason))


async def _kill_switch_async(reason: str | None) -> None:
    settings = get_runday_settings()
    env_path = Path(".env")

    # Read existing .env
    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
    else:
        content = ""

    # Replace or append FNO_MODULE_ENABLED
    lines = content.splitlines()
    replaced = False
    new_lines = []
    for line in lines:
        if line.startswith("FNO_MODULE_ENABLED="):
            new_lines.append("FNO_MODULE_ENABLED=false")
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        new_lines.append("FNO_MODULE_ENABLED=false")

    new_content = "\n".join(new_lines) + "\n"

    # Atomic write: tempfile in same directory + rename
    dir_ = env_path.parent
    fd, tmp_path = tempfile.mkstemp(dir=dir_, prefix=".env.tmp.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_content)
        os.replace(tmp_path, env_path)
    except Exception:
        os.unlink(tmp_path)
        raise

    _console.print("[bold yellow]Kill-switch armed.[/bold yellow]")
    _console.print("  • FNO_MODULE_ENABLED=false written to .env (atomic)")

    # Find orchestrator PID
    pid = _find_orchestrator_pid(settings.runday_pidfile_path)
    if pid:
        _console.print(f"  • Orchestrator PID: [bold]{pid}[/bold]")
        _console.print(f"\n[bold]Now run:[/bold]  kill -TERM {pid}")
    else:
        _console.print("  • Orchestrator PID not found (check pidfile or use pgrep python)")
        _console.print("\n[bold]Now run:[/bold]  kill -TERM <pid>")

    if reason:
        _console.print(f"\n  Reason: {reason}")

    # Send Telegram alert
    telegram = TelegramReporter(settings)
    await telegram.send_kill_switch_alert(reason=reason)
    _console.print("\n  🛑 Telegram alert sent.")


def _find_orchestrator_pid(pidfile: str) -> int | None:
    """Try pidfile first, then pgrep."""
    try:
        pid_text = Path(pidfile).read_text().strip()
        pid = int(pid_text)
        # Verify the PID is actually alive
        os.kill(pid, 0)
        return pid
    except Exception:
        pass
    try:
        import subprocess
        result = subprocess.run(
            ["pgrep", "-f", "src.main"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return int(result.stdout.strip().splitlines()[0])
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

@app.command()
def report(
    report_date: Annotated[Optional[str], typer.Option("--date", help="Report date YYYY-MM-DD (default: today)")] = None,
    json: Annotated[bool, typer.Option("--json", help="Emit full structured JSON")] = False,
    markdown: Annotated[bool, typer.Option("--markdown", help="Write Markdown to reports/runday-YYYY-MM-DD.md")] = False,
    telegram: Annotated[bool, typer.Option("--telegram", help="Send executive summary to Telegram")] = False,
) -> None:
    """End-of-day rollup. Reads from DB only — no external calls. Runs in <5s."""
    asyncio.run(
        _report_async(
            report_date=report_date,
            emit_json=json,
            write_markdown=markdown,
            send_telegram=telegram,
        )
    )


async def _report_async(
    report_date: str | None,
    emit_json: bool,
    write_markdown: bool,
    send_telegram: bool,
) -> None:
    from src.runday.scripts.daily_report import build_report, format_markdown_report

    target_date = date.today()
    if report_date:
        try:
            target_date = date.fromisoformat(report_date)
        except ValueError:
            _console.print(f"[red]Invalid --date '{report_date}'. Use YYYY-MM-DD.[/red]")
            raise SystemExit(1)

    _console.print(f"Building report for [bold]{target_date.isoformat()}[/bold]…")
    data = await build_report(target_date)

    markdown_path: str | None = None
    if write_markdown:
        md_dir = Path("reports")
        md_dir.mkdir(exist_ok=True)
        markdown_path = str(md_dir / f"runday-{target_date.isoformat()}.md")
        Path(markdown_path).write_text(format_markdown_report(data), encoding="utf-8")
        _console.print(f"Markdown report written to [bold]{markdown_path}[/bold]")

    if emit_json:
        print(json_out.emit_report(data))
        return

    _render_report_console(data)

    if send_telegram:
        settings = get_runday_settings()
        tg = TelegramReporter(settings)
        await tg.send_eod_summary(data, markdown_path=markdown_path)
        _console.print("Telegram summary sent.")


def _render_report_console(data: dict) -> None:
    """Render the daily report to console."""
    from rich.table import Table

    date_str = data.get("date", "unknown")
    _console.rule(f"[bold cyan]LAABH DAILY REPORT — {date_str}[/bold cyan]")
    _console.print()

    # Pipeline completeness
    pc = data.get("pipeline_completeness", {})
    _console.print("[bold cyan]PIPELINE COMPLETENESS[/bold cyan]")
    _console.print(f"  Ran: {pc.get('ran', 0)}/{pc.get('total_scheduled', 0)} scheduled jobs")
    if pc.get("skipped"):
        _console.print(f"  Skipped: [yellow]{', '.join(pc['skipped'])}[/yellow]")
    _console.print()

    # Chain health
    ch = data.get("chain_health", {})
    _console.print("[bold cyan]DATA INGESTION HEALTH[/bold cyan]")
    total = ch.get("total", 0)
    ok_pct = ch.get("ok_pct", 0)
    ms_pct = ch.get("missed_pct", 0)
    nse = ch.get("nse_share_pct", 0)
    ms_color = "red" if ms_pct > 5 else "green"
    _console.print(
        f"  Chain: {total} attempts | ok={ok_pct:.1f}% | "
        f"missed=[{ms_color}]{ms_pct:.1f}%[/{ms_color}] | "
        f"NSE share={nse:.1f}%"
    )
    for issue in ch.get("issues", []):
        _console.print(
            f"  Issues [{issue['type']}]: {issue['count']} total, {issue['filed']} filed"
        )
    _console.print()

    # LLM
    llm = data.get("llm_activity", {})
    _console.print("[bold cyan]LLM ACTIVITY[/bold cyan]")
    _console.print(
        f"  {llm.get('total_rows', 0)} calls across {len(llm.get('callers', []))} callers | "
        f"tokens in={llm.get('total_tokens_in', 0):,} out={llm.get('total_tokens_out', 0):,} | "
        f"est. cost ~${llm.get('estimated_cost_usd', 0):.4f}"
    )
    _console.print()

    # Trading
    tr = data.get("trading", {})
    _console.print("[bold cyan]TRADING[/bold cyan]")
    pnl = tr.get("day_pnl", 0)
    pnl_color = "green" if pnl >= 0 else "red"
    _console.print(
        f"  proposed={tr.get('proposed', 0)} filled={tr.get('filled', 0)} "
        f"closed(T/S/Ti)={tr.get('closed_target', 0)}/{tr.get('closed_stop', 0)}/{tr.get('closed_time', 0)} "
        f"P&L=[{pnl_color}]₹{pnl:,.0f}[/{pnl_color}]"
    )

    dq = tr.get("decision_quality", [])
    if dq:
        _console.print()
        _console.print("[bold]Decision Quality[/bold]")
        table = Table(box=None, padding=(0, 1))
        table.add_column("Symbol")
        table.add_column("Strategy")
        table.add_column("Status")
        table.add_column("P&L", justify="right")
        table.add_column("Thesis")
        for row in dq:
            pnl_v = row.get("final_pnl")
            pnl_s = f"₹{pnl_v:,.0f}" if pnl_v is not None else "n/a"
            thesis = (row.get("thesis_excerpt") or "")[:80]
            table.add_row(row["symbol"], row["strategy"], row["status"], pnl_s, thesis)
        _console.print(table)
    _console.print()

    # Surprises
    surprises = data.get("surprises", [])
    if surprises:
        _console.print("[bold yellow]SURPRISES[/bold yellow]")
        for s in surprises:
            _console.print(f"  ⚠️  {s}")
        _console.print()
    else:
        _console.print("[green]No surprises detected.[/green]")


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------

@app.command()
def replay(
    date_str: Annotated[str, typer.Option("--date", help="Replay date YYYY-MM-DD")] = "",
    mock_llm: Annotated[bool, typer.Option("--mock-llm/--live-llm", help="Use cached LLM results (default: mock)")] = True,
    out: Annotated[str, typer.Option("--out", help="Output directory for the report")] = "reports",
    json_out_flag: Annotated[bool, typer.Option("--json", help="Emit structured JSON to stdout")] = False,
) -> None:
    """Replay the full F&O daily routine for a historical date.

    Exits 0 (clean), 10 (gate WARN), 20 (gate FAIL).
    """
    if not date_str:
        _console.print("[red]--date is required for replay (e.g. --date 2026-04-23)[/red]")
        raise SystemExit(1)

    try:
        target_date = date.fromisoformat(date_str)
    except ValueError:
        _console.print(f"[red]Invalid --date '{date_str}'. Use YYYY-MM-DD format.[/red]")
        raise SystemExit(1)

    code = asyncio.run(_replay_async(target_date=target_date, mock_llm=mock_llm, out_dir=out, emit_json=json_out_flag))
    raise SystemExit(code)


async def _replay_async(
    target_date: date,
    mock_llm: bool,
    out_dir: str,
    emit_json: bool,
) -> int:
    import uuid as _uuid
    from src.dryrun.orchestrator import ReplayGateFailed, replay as _replay
    from src.runday.checks.base import Severity
    from src.runday.scripts.daily_report import build_report, format_markdown_report

    run_id = _uuid.uuid4()
    _console.print(f"[bold]Replaying {target_date.isoformat()}[/bold] run_id={str(run_id)[:8]}")

    try:
        result = await _replay(target_date, mock_llm=mock_llm, run_id=run_id)
    except ReplayGateFailed as exc:
        _console.print(f"[red]Replay gate FAILED: {exc}[/red]")
        return 20
    except Exception as exc:
        _console.print(f"[red]Replay error: {exc}[/red]")
        return 20

    # Build and write the report
    data = await build_report(target_date, dryrun_run_id=run_id)

    run_short = str(run_id)[:8]
    md_path: str | None = None
    if out_dir:
        from pathlib import Path
        out_path = Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        md_path = str(out_path / f"replay-{target_date.isoformat()}-{run_short}.md")
        Path(md_path).write_text(format_markdown_report(data), encoding="utf-8")
        _console.print(f"Report: [bold]{md_path}[/bold]")

    if emit_json:
        print(json_out.emit_report(data))
        return 0

    _render_report_console(data)

    # Show captured side-effects
    captures = result.captures
    telegram_count = sum(1 for c in captures if c.get("type") == "telegram")
    _console.print(f"\n[cyan]Captured Telegrams: {telegram_count} (suppressed)[/cyan]")
    if result.gates_failed:
        _console.print(f"[red]Gates failed: {', '.join(result.gates_failed)}[/red]")
        return 20
    if result.gates_warned:
        _console.print(f"[yellow]Gates warned: {', '.join(result.gates_warned)}[/yellow]")
        return 10

    return 0


def main() -> None:
    """Entry point for the laabh-runday console script."""
    app()
