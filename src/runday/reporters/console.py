"""Rich-based console reporter for laabh-runday."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pytz
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from src.runday.checks.base import CheckResult, Severity

_IST = pytz.timezone("Asia/Kolkata")
# legacy_windows=False forces ANSI escape rendering instead of the Win32
# Console API path, which on classic conhost.exe crashes with UnicodeEncodeError
# the moment Rich tries to draw the ✓/✗/⚠ glyphs through the cp1252 codec.
# Modern Windows terminals (Windows Terminal, VS Code, PowerShell 7+, and
# even conhost.exe on Win10+ with VT enabled) all handle ANSI correctly.
_CONSOLE = Console(legacy_windows=False)

_ICONS = {
    Severity.OK: "[bold green]✓[/bold green]",
    Severity.WARN: "[bold yellow]⚠[/bold yellow]",
    Severity.FAIL: "[bold red]✗[/bold red]",
}

_COLORS = {
    Severity.OK: "green",
    Severity.WARN: "yellow",
    Severity.FAIL: "red",
}


def render_check_list(results: list[CheckResult], title: str = "") -> None:
    """Render a list of CheckResult objects to the console."""
    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    table.add_column("", width=3)
    table.add_column("Check", style="dim", no_wrap=True)
    table.add_column("Message")
    table.add_column("ms", justify="right", style="dim", width=6)

    for r in results:
        icon = _ICONS[r.severity]
        color = _COLORS[r.severity]
        table.add_row(
            icon,
            r.name,
            f"[{color}]{r.message}[/{color}]",
            str(r.duration_ms) if r.duration_ms else "-",
        )

    if title:
        _CONSOLE.print(Panel(table, title=f"[bold]{title}[/bold]", expand=False))
    else:
        _CONSOLE.print(table)


def render_summary_line(results: list[CheckResult]) -> None:
    """Print a one-line pass/fail summary."""
    fails = [r for r in results if r.severity == Severity.FAIL]
    warns = [r for r in results if r.severity == Severity.WARN]
    oks = [r for r in results if r.severity == Severity.OK]

    if fails:
        _CONSOLE.print(
            f"[bold red]FAIL[/bold red] — {len(fails)} failed, {len(warns)} warned, {len(oks)} passed"
        )
    elif warns:
        _CONSOLE.print(
            f"[bold yellow]WARN[/bold yellow] — {len(warns)} warnings, {len(oks)} passed"
        )
    else:
        _CONSOLE.print(f"[bold green]ALL PASS[/bold green] — {len(oks)} checks passed")


def render_status_dashboard(data: dict[str, Any]) -> None:
    """Render the live status dashboard."""
    now_ist = datetime.now(_IST).strftime("%d %b %Y %H:%M IST")
    _CONSOLE.rule(f"[bold cyan]LAABH STATUS — {now_ist}[/bold cyan]")
    _CONSOLE.print()

    # Chain collection
    chain = data.get("chain", {})
    if chain:
        _section_header("CHAIN COLLECTION (last 10 min)")
        total = chain.get("total", 0)
        ok_c = chain.get("ok", 0)
        fb_c = chain.get("fallback", 0)
        ms_c = chain.get("missed", 0)
        ok_pct = chain.get("ok_pct", 0)
        fb_pct = chain.get("fallback_pct", 0)
        ms_pct = chain.get("missed_pct", 0)
        nse_share = chain.get("nse_share_pct", 0)
        t1_p95 = chain.get("tier1_p95_latency_ms", "-")
        t2_p95 = chain.get("tier2_p95_latency_ms", "-")
        color = "green" if ms_pct < 5 else "red"
        _CONSOLE.print(
            f"  attempts: [bold]{total}[/bold]   "
            f"ok: [green]{ok_c} ({ok_pct:.0f}%)[/green]   "
            f"fallback: [yellow]{fb_c} ({fb_pct:.0f}%)[/yellow]   "
            f"missed: [{color}]{ms_c} ({ms_pct:.0f}%)[/{color}]"
        )
        _CONSOLE.print(
            f"  nse share: [bold]{nse_share:.1f}%[/bold]"
            f"   tier1 latency p95: {t1_p95}ms"
            f"   tier2 latency p95: {t2_p95}ms"
        )
        _CONSOLE.print()

    # Source health
    sources = data.get("sources", {})
    if sources:
        _section_header("SOURCE HEALTH")
        for src_name, info in sources.items():
            status = info.get("status", "unknown")
            color = "green" if status == "healthy" else "red"
            last_err = info.get("last_error_at") or "never"
            _CONSOLE.print(f"  {src_name:<12} [{color}]{status}[/{color}]   (last err: {last_err})")
        _CONSOLE.print()

    # Open issues
    issues = data.get("issues", {})
    _section_header("OPEN ISSUES")
    schema_mm = issues.get("schema_mismatch", 0)
    sustained = issues.get("sustained_failure", 0)
    total_i = issues.get("total", 0)
    color = "red" if total_i > 0 else "green"
    _CONSOLE.print(
        f"  schema_mismatch: [{color}]{schema_mm}[/{color}]"
        f"   sustained_failure: [{color}]{sustained}[/{color}]"
        f"   total: [{color}]{total_i}[/{color}]"
    )
    _CONSOLE.print()

    # Pipeline today
    pipeline = data.get("pipeline", {})
    if pipeline:
        _section_header("PIPELINE TODAY")
        p1 = pipeline.get("phase1", {})
        p2 = pipeline.get("phase2", {})
        p3 = pipeline.get("phase3", {})
        brief = pipeline.get("morning_brief", {})
        vix = pipeline.get("vix", {})
        ban = pipeline.get("ban_list_count", 0)

        _render_pipeline_row(p1, "phase 1", p2, "phase 2")
        _render_pipeline_row(p3, "phase 3", brief, "morning brief")

        vix_val = vix.get("value", "n/a")
        vix_regime = vix.get("regime", "n/a")
        _CONSOLE.print(
            f"  vix regime: [bold]{vix_regime} (VIX={vix_val})[/bold]"
            f"   ban list: [yellow]{ban}[/yellow] names"
        )
        _CONSOLE.print()

    # Trading today
    trading = data.get("trading", {})
    if trading:
        _section_header("TRADING TODAY")
        proposed = trading.get("proposed", 0)
        filled = trading.get("filled", 0)
        scaled = trading.get("scaled_out", 0)
        c_target = trading.get("closed_target", 0)
        c_stop = trading.get("closed_stop", 0)
        c_time = trading.get("closed_time", 0)
        open_pos = trading.get("open_positions", 0)
        max_pos = trading.get("max_open_positions", 3)
        pnl = trading.get("day_pnl", 0)
        pnl_color = "green" if pnl >= 0 else "red"

        _CONSOLE.print(
            f"  proposed: {proposed}   filled: {filled}   scaled out: {scaled}   "
            f"closed (target): {c_target}   closed (stop): {c_stop}   closed (time): {c_time}"
        )
        pos_color = "red" if open_pos > max_pos else "green"
        _CONSOLE.print(
            f"  open positions: [{pos_color}]{open_pos} / {max_pos} max[/{pos_color}]"
            f"   day P&L: [{pnl_color}]₹{pnl:,.0f} (paper)[/{pnl_color}]"
        )
        _CONSOLE.print()

    # Next jobs
    jobs = data.get("next_jobs", [])
    if jobs:
        _section_header("RECENT JOBS")
        for job in jobs[:5]:
            _CONSOLE.print(f"  {job['name']:<40} last run: {job.get('last_run', 'never')}")
        _CONSOLE.print()


def _section_header(title: str) -> None:
    _CONSOLE.print(f"[bold cyan]{title}[/bold cyan]")


def _render_pipeline_row(
    left: dict[str, Any],
    left_label: str,
    right: dict[str, Any],
    right_label: str,
) -> None:
    left_ok = left.get("ok", False)
    left_val = left.get("value", "")
    right_ok = right.get("ok", False)
    right_val = right.get("value", "")

    l_icon = "[green]✓[/green]" if left_ok else "[red]✗[/red]"
    r_icon = "[green]✓[/green]" if right_ok else "[red]✗[/red]"

    _CONSOLE.print(
        f"  {left_label}: {l_icon} {left_val:<25}   "
        f"{right_label}: {r_icon} {right_val}"
    )


def get_console() -> Console:
    """Return the shared Rich console."""
    return _CONSOLE
