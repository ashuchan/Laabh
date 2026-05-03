"""Telegram message templates for laabh-runday."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
import pytz

from src.runday.checks.base import CheckResult, Severity
from src.runday.config import RundaySettings

_IST = pytz.timezone("Asia/Kolkata")
_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
_MAX_MSG_LEN = 4096
_MDV2_ESCAPE = str.maketrans({c: f"\\{c}" for c in r"_*[]()~`>#+-=|{}.!"})


def _esc(s: str) -> str:
    """Escape a string for Telegram MarkdownV2 (does not preserve formatting)."""
    return s.translate(_MDV2_ESCAPE)


class TelegramReporter:
    """Send formatted runday messages to Telegram."""

    def __init__(self, settings: RundaySettings) -> None:
        self._settings = settings

    async def send_preflight_ok(self, results: list[CheckResult]) -> None:
        """Send a ≤3-line preflight-success message."""
        now_ist = datetime.now(_IST).strftime("%H:%M IST")
        ok_count = sum(1 for r in results if r.severity == Severity.OK)
        warn_count = sum(1 for r in results if r.severity == Severity.WARN)
        lines = [
            f"🟢 *Laabh preflight OK* at {_esc(now_ist)}",
            _esc(f"{ok_count} checks passed" + (f", {warn_count} warnings" if warn_count else "")),
        ]
        if warn_count:
            warn_names = [r.name for r in results if r.severity == Severity.WARN]
            lines.append(_esc(f"Warnings: {', '.join(warn_names)}"))
        await self._send("\n".join(lines))

    async def send_preflight_fail(self, results: list[CheckResult]) -> None:
        """Send a bullet list of failed checks — fires even with --quiet."""
        now_ist = datetime.now(_IST).strftime("%H:%M IST")
        fail_results = [r for r in results if r.severity == Severity.FAIL]
        lines = [f"🔴 *Laabh preflight FAIL* at {_esc(now_ist)}", ""]
        for r in fail_results:
            lines.append(f"• *{_esc(r.name)}*: {_esc(r.message)}")
        await self._send("\n".join(lines))

    async def send_eod_summary(
        self,
        report_data: dict[str, Any],
        markdown_path: str | None = None,
    ) -> None:
        """Send end-of-day executive summary (truncated at 4096 chars)."""
        date_str = report_data.get("date", datetime.now(_IST).strftime("%Y-%m-%d"))
        sections = []
        sections.append(f"📊 *Laabh EOD Report — {_esc(date_str)}*\n")

        pipeline = report_data.get("pipeline_completeness", {})
        if pipeline:
            total_jobs = pipeline.get("total_scheduled", 0)
            ran = pipeline.get("ran", 0)
            sections.append(f"*Pipeline:* {_esc(f'{ran}/{total_jobs} jobs ran')}")

        chain = report_data.get("chain_health", {})
        if chain:
            ok_pct = chain.get("ok_pct", 0)
            missed_pct = chain.get("missed_pct", 0)
            sections.append(f"*Chain:* {_esc(f'ok={ok_pct:.0f}% missed={missed_pct:.1f}%')}")

        llm = report_data.get("llm_activity", {})
        if llm:
            total_calls = llm.get("total_rows", 0)
            cost = llm.get("estimated_cost_usd", 0)
            sections.append(f"*LLM:* {_esc(f'{total_calls} calls ~${cost:.4f}')}")

        trading = report_data.get("trading", {})
        if trading:
            filled = trading.get("filled", 0)
            pnl = trading.get("day_pnl", 0)
            pnl_sign = "+" if pnl >= 0 else ""
            sections.append(f"*Trading:* {_esc(f'{filled} filled | P&L {pnl_sign}₹{pnl:,.0f}')}")

        surprises = report_data.get("surprises", [])
        if surprises:
            sections.append(f"\n⚠️ *Surprises {_esc(f'({len(surprises)})')}:*")
            for s in surprises[:5]:
                sections.append(f"• {_esc(str(s))}")

        if markdown_path:
            sections.append(f"\nReport: `{_esc(markdown_path)}`")

        msg = "\n".join(sections)
        if len(msg) > _MAX_MSG_LEN:
            msg = msg[: _MAX_MSG_LEN - 20] + "\n…[truncated]"
        await self._send(msg)

    async def send_kill_switch_alert(self, reason: str | None = None) -> None:
        """Send kill-switch armed alert."""
        now_ist = datetime.now(_IST).strftime("%H:%M IST")
        parts = [f"🛑 *F\\&O kill\\-switch armed* by operator at {_esc(now_ist)}"]
        if reason:
            parts.append(_esc(f"Reason: {reason}"))
        await self._send("\n".join(parts))

    async def _send(self, text: str) -> None:
        token = self._settings.telegram_bot_token
        chat_id = self._settings.telegram_chat_id
        if not token or not chat_id:
            return
        url = _TELEGRAM_API.format(token=token)
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                url,
                json={"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"},
            )
            r.raise_for_status()
