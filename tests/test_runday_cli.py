"""Tests for the laabh-runday CLI entry points."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from src.runday.checks.base import CheckResult, Severity
from src.runday.cli import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok_check(name: str = "test.check") -> CheckResult:
    return CheckResult(name=name, severity=Severity.OK, message="all good")


def _fail_check(name: str = "test.check") -> CheckResult:
    return CheckResult(name=name, severity=Severity.FAIL, message="broken")


# ---------------------------------------------------------------------------
# --help
# ---------------------------------------------------------------------------

def test_app_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "preflight" in result.output
    assert "checkpoint" in result.output
    assert "status" in result.output
    assert "report" in result.output


def test_preflight_help():
    result = runner.invoke(app, ["preflight", "--help"])
    assert result.exit_code == 0
    assert "--quiet" in result.output
    assert "--json" in result.output
    assert "--skip" in result.output


def test_checkpoint_help():
    result = runner.invoke(app, ["checkpoint", "--help"])
    assert result.exit_code == 0
    assert "--since" in result.output
    assert "--strict" in result.output


def test_status_help():
    result = runner.invoke(app, ["status", "--help"])
    assert result.exit_code == 0
    assert "--watch" in result.output
    assert "--json" in result.output


def test_tier_check_help():
    result = runner.invoke(app, ["tier-check", "--help"])
    assert result.exit_code == 0


def test_kill_switch_help():
    result = runner.invoke(app, ["kill-switch", "--help"])
    assert result.exit_code == 0
    assert "--reason" in result.output


def test_report_help():
    result = runner.invoke(app, ["report", "--help"])
    assert result.exit_code == 0
    assert "--date" in result.output
    assert "--markdown" in result.output
    assert "--telegram" in result.output


# ---------------------------------------------------------------------------
# preflight --json
# ---------------------------------------------------------------------------

@patch("src.runday.cli.get_runday_settings")
@patch("src.runday.cli.TelegramReporter")
def test_preflight_json_output(mock_tg_cls, mock_settings_fn):
    import json

    settings = MagicMock()
    settings.runday_telegram_on_preflight_ok = False
    mock_settings_fn.return_value = settings
    mock_tg = AsyncMock()
    mock_tg_cls.return_value = mock_tg

    all_checks = [
        "src.runday.cli.EnvCheck",
        "src.runday.cli.DBConnectivityCheck",
        "src.runday.cli.MigrationsCurrentCheck",
        "src.runday.cli.RequiredTablesCheck",
        "src.runday.cli.SeedDataCheck",
        "src.runday.cli.AnthropicCheck",
        "src.runday.cli.TelegramCheck",
        "src.runday.cli.AngelOneCheck",
        "src.runday.cli.NSECheck",
        "src.runday.cli.DhanCheck",
        "src.runday.cli.GitHubCheck",
        "src.runday.cli.TierTableCheck",
        "src.runday.cli.TradingDayCheck",
    ]

    mock_check_inst = AsyncMock()
    mock_check_inst.name = "test.check"
    mock_check_inst.run = AsyncMock(return_value=_ok_check())

    with patch.multiple("src.runday.cli", **{
        cls.split(".")[-1]: MagicMock(return_value=mock_check_inst)
        for cls in all_checks
    }):
        result = runner.invoke(app, ["preflight", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "checks" in data
    assert "summary" in data


# ---------------------------------------------------------------------------
# checkpoint
# ---------------------------------------------------------------------------

@patch("src.runday.cli.get_runday_settings")
@patch("src.runday.cli.make_phase_check")
def test_checkpoint_phase1_pass(mock_make, mock_settings_fn):
    settings = MagicMock()
    mock_settings_fn.return_value = settings

    mock_check = AsyncMock()
    mock_check.run = AsyncMock(return_value=_ok_check("checkpoint.phase1"))
    mock_make.return_value = mock_check

    result = runner.invoke(app, ["checkpoint", "phase1"])
    assert result.exit_code == 0


@patch("src.runday.cli.get_runday_settings")
@patch("src.runday.cli.make_phase_check")
def test_checkpoint_phase1_fail(mock_make, mock_settings_fn):
    settings = MagicMock()
    mock_settings_fn.return_value = settings

    mock_check = AsyncMock()
    mock_check.run = AsyncMock(return_value=_fail_check("checkpoint.phase1"))
    mock_make.return_value = mock_check

    result = runner.invoke(app, ["checkpoint", "phase1"])
    assert result.exit_code == 20


def test_checkpoint_invalid_phase():
    result = runner.invoke(app, ["checkpoint", "not-a-phase"])
    assert result.exit_code == 1


def test_checkpoint_since_invalid_date():
    result = runner.invoke(app, ["checkpoint", "phase1", "--since", "not-a-date"])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# kill-switch
# ---------------------------------------------------------------------------

@patch("src.runday.cli.get_runday_settings")
@patch("src.runday.cli.TelegramReporter")
def test_kill_switch_writes_env(mock_tg_cls, mock_settings_fn, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("FNO_MODULE_ENABLED=true\nANTHROPIC_API_KEY=test\n")

    settings = MagicMock()
    settings.telegram_bot_token = ""
    settings.telegram_chat_id = ""
    settings.runday_pidfile_path = str(tmp_path / "laabh.pid")
    mock_settings_fn.return_value = settings

    mock_tg = AsyncMock()
    mock_tg_cls.return_value = mock_tg

    result = runner.invoke(app, ["kill-switch"])
    assert result.exit_code == 0

    content = env_file.read_text()
    assert "FNO_MODULE_ENABLED=false" in content
    assert "FNO_MODULE_ENABLED=true" not in content


@patch("src.runday.cli.get_runday_settings")
@patch("src.runday.cli.TelegramReporter")
def test_kill_switch_appends_if_missing(mock_tg_cls, mock_settings_fn, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=postgresql://test\n")

    settings = MagicMock()
    settings.telegram_bot_token = ""
    settings.telegram_chat_id = ""
    settings.runday_pidfile_path = str(tmp_path / "laabh.pid")
    mock_settings_fn.return_value = settings

    mock_tg = AsyncMock()
    mock_tg_cls.return_value = mock_tg

    result = runner.invoke(app, ["kill-switch"])
    assert result.exit_code == 0
    content = env_file.read_text()
    assert "FNO_MODULE_ENABLED=false" in content


# ---------------------------------------------------------------------------
# report --markdown
# ---------------------------------------------------------------------------

@patch("src.runday.cli.get_runday_settings")
@patch("src.runday.scripts.daily_report.build_report")
def test_report_markdown_writes_file(mock_build, mock_settings_fn, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    settings = MagicMock()
    mock_settings_fn.return_value = settings

    mock_build.return_value = {
        "date": "2026-04-27",
        "pipeline_completeness": {"total_scheduled": 10, "ran": 10, "skipped": [], "jobs": {}},
        "chain_health": {"total": 200, "ok": 190, "fallback": 8, "missed": 2,
                         "ok_pct": 95.0, "fallback_pct": 4.0, "missed_pct": 1.0,
                         "nse_share_pct": 95.0, "by_status": {}, "issues": []},
        "llm_activity": {"callers": [], "total_rows": 0, "total_tokens_in": 0,
                         "total_tokens_out": 0, "estimated_cost_usd": 0.0},
        "trading": {"proposed": 0, "filled": 0, "scaled_out": 0,
                    "closed_target": 0, "closed_stop": 0, "closed_time": 0,
                    "day_pnl": 0.0, "by_strategy": {}, "by_status": {}, "decision_quality": []},
        "candidates": {},
        "vix_stats": {},
        "source_health": [],
        "surprises": [],
    }

    # build_report is imported inside _report_async; patch at source module
    result = runner.invoke(app, ["report", "--date", "2026-04-27", "--markdown"])

    assert result.exit_code == 0
    report_file = tmp_path / "reports" / "runday-2026-04-27.md"
    assert report_file.exists()
    content = report_file.read_text()
    assert "# Laabh Daily Report" in content
