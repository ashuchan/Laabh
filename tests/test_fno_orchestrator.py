"""Tests for F&O orchestrator — pipeline coordination with mocked phases."""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.fno.universe import LiquidityResult


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _liq(passed: bool, symbol: str = "NIFTY") -> LiquidityResult:
    return LiquidityResult(
        instrument_id=str(uuid.uuid4()),
        symbol=symbol,
        passed=passed,
        atm_oi=60000 if passed else 1000,
    )


def _phase2_result(passed: bool, symbol: str = "NIFTY"):
    from src.fno.catalyst_scorer import Phase2Result
    return Phase2Result(
        instrument_id=str(uuid.uuid4()),
        symbol=symbol,
        passed=passed,
        composite_score=7.5 if passed else 3.0,
    )


def _phase3_result(decision: str, symbol: str = "NIFTY"):
    from src.fno.thesis_synthesizer import ThesisResult
    return ThesisResult(
        instrument_id=str(uuid.uuid4()),
        symbol=symbol,
        decision=decision,
        direction="bullish",
        thesis="Test thesis.",
        risk_factors=["gap risk"],
        confidence=0.75,
    )


RUN_DATE = date(2026, 4, 27)


# ---------------------------------------------------------------------------
# run_premarket_pipeline
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_premarket_pipeline_disabled_returns_skipped() -> None:
    from src.fno import orchestrator

    with patch.object(orchestrator._settings, "fno_module_enabled", False):
        result = await orchestrator.run_premarket_pipeline(RUN_DATE)

    assert result.get("skipped") is True


@pytest.mark.asyncio
async def test_premarket_pipeline_runs_all_three_phases() -> None:
    from src.fno import orchestrator

    p1_results = [_liq(True, "NIFTY"), _liq(True, "RELIANCE"), _liq(False, "ZOMATO")]
    p2_results = [_phase2_result(True, "NIFTY"), _phase2_result(False, "RELIANCE")]
    p3_results = [_phase3_result("PROCEED", "NIFTY"), _phase3_result("SKIP", "RELIANCE")]

    with (
        patch.object(orchestrator._settings, "fno_module_enabled", True),
        patch("src.fno.orchestrator.run_phase1", AsyncMock(return_value=p1_results)),
        patch("src.fno.orchestrator.run_phase2", AsyncMock(return_value=p2_results)),
        patch("src.fno.orchestrator.run_phase3", AsyncMock(return_value=p3_results)),
    ):
        result = await orchestrator.run_premarket_pipeline(RUN_DATE)

    assert result["phase1_total"] == 3
    assert result["phase1_passed"] == 2
    assert result["phase2_total"] == 2
    assert result["phase2_passed"] == 1
    assert result["phase3_total"] == 2
    assert result["phase3_proceed"] == 1
    assert result["run_date"] == RUN_DATE.isoformat()


@pytest.mark.asyncio
async def test_premarket_pipeline_empty_universe() -> None:
    from src.fno import orchestrator

    with (
        patch.object(orchestrator._settings, "fno_module_enabled", True),
        patch("src.fno.orchestrator.run_phase1", AsyncMock(return_value=[])),
        patch("src.fno.orchestrator.run_phase2", AsyncMock(return_value=[])),
        patch("src.fno.orchestrator.run_phase3", AsyncMock(return_value=[])),
    ):
        result = await orchestrator.run_premarket_pipeline(RUN_DATE)

    assert result["phase1_passed"] == 0
    assert result["phase3_proceed"] == 0


@pytest.mark.asyncio
async def test_premarket_pipeline_phase1_exception_propagates() -> None:
    """If Phase 1 raises, the pipeline propagates the exception (no silent swallow)."""
    from src.fno import orchestrator

    with (
        patch.object(orchestrator._settings, "fno_module_enabled", True),
        patch("src.fno.orchestrator.run_phase1", AsyncMock(side_effect=RuntimeError("DB down"))),
    ):
        with pytest.raises(RuntimeError, match="DB down"):
            await orchestrator.run_premarket_pipeline(RUN_DATE)


@pytest.mark.asyncio
async def test_premarket_uses_today_when_no_date_given() -> None:
    from src.fno import orchestrator
    from datetime import date as _date
    import datetime

    captured = {}

    async def _mock_p1(run_date, *, as_of=None):
        captured["run_date"] = run_date
        return []

    with (
        patch.object(orchestrator._settings, "fno_module_enabled", True),
        patch("src.fno.orchestrator.run_phase1", side_effect=_mock_p1),
        patch("src.fno.orchestrator.run_phase2", AsyncMock(return_value=[])),
        patch("src.fno.orchestrator.run_phase3", AsyncMock(return_value=[])),
    ):
        await orchestrator.run_premarket_pipeline()

    assert captured["run_date"] == _date.today()


# ---------------------------------------------------------------------------
# run_chain_refresh
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chain_refresh_disabled_returns_zero() -> None:
    from src.fno import orchestrator

    with patch.object(orchestrator._settings, "fno_module_enabled", False):
        count = await orchestrator.run_chain_refresh()

    assert count == 0


@pytest.mark.asyncio
async def test_chain_refresh_returns_count() -> None:
    from src.fno import orchestrator

    with (
        patch.object(orchestrator._settings, "fno_module_enabled", True),
        patch("src.fno.orchestrator.collect_chains", AsyncMock(return_value=5)),
    ):
        count = await orchestrator.run_chain_refresh()

    assert count == 5


# ---------------------------------------------------------------------------
# run_vix_refresh
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_vix_refresh_disabled_skips() -> None:
    from src.fno import orchestrator

    mock_vix = AsyncMock()
    mock_ban = AsyncMock()
    with (
        patch.object(orchestrator._settings, "fno_module_enabled", False),
        patch("src.fno.orchestrator.collect_vix", mock_vix),
        patch("src.fno.orchestrator.fetch_ban_list", mock_ban),
    ):
        await orchestrator.run_vix_refresh()

    mock_vix.assert_not_called()
    mock_ban.assert_not_called()


@pytest.mark.asyncio
async def test_vix_refresh_calls_vix_and_ban_list() -> None:
    from src.fno import orchestrator

    mock_vix = AsyncMock()
    mock_ban = AsyncMock()
    with (
        patch.object(orchestrator._settings, "fno_module_enabled", True),
        patch("src.fno.orchestrator.collect_vix", mock_vix),
        patch("src.fno.orchestrator.fetch_ban_list", mock_ban),
    ):
        await orchestrator.run_vix_refresh()

    mock_vix.assert_called_once()
    mock_ban.assert_called_once()


@pytest.mark.asyncio
async def test_vix_refresh_swallows_vix_exception() -> None:
    """VIX failure should not propagate — ban list should still run."""
    from src.fno import orchestrator

    mock_ban = AsyncMock()
    with (
        patch.object(orchestrator._settings, "fno_module_enabled", True),
        patch("src.fno.orchestrator.collect_vix", AsyncMock(side_effect=RuntimeError("VIX API down"))),
        patch("src.fno.orchestrator.fetch_ban_list", mock_ban),
    ):
        await orchestrator.run_vix_refresh()  # should not raise

    mock_ban.assert_called_once()


@pytest.mark.asyncio
async def test_vix_refresh_swallows_ban_list_exception() -> None:
    """Ban list failure should not propagate."""
    from src.fno import orchestrator

    with (
        patch.object(orchestrator._settings, "fno_module_enabled", True),
        patch("src.fno.orchestrator.collect_vix", AsyncMock()),
        patch("src.fno.orchestrator.fetch_ban_list", AsyncMock(side_effect=RuntimeError("NSE down"))),
    ):
        await orchestrator.run_vix_refresh()  # should not raise


# ---------------------------------------------------------------------------
# run_eod_tasks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_eod_tasks_disabled_skips() -> None:
    from src.fno import orchestrator

    mock_iv = AsyncMock(return_value=0)
    with (
        patch.object(orchestrator._settings, "fno_module_enabled", False),
        patch("src.fno.orchestrator.build_for_date", mock_iv),
    ):
        await orchestrator.run_eod_tasks(RUN_DATE)

    mock_iv.assert_not_called()


@pytest.mark.asyncio
async def test_eod_tasks_calls_iv_builder() -> None:
    from src.fno import orchestrator

    mock_iv = AsyncMock(return_value=12)
    with (
        patch.object(orchestrator._settings, "fno_module_enabled", True),
        patch("src.fno.orchestrator.build_for_date", mock_iv),
        patch("src.fno.orchestrator._send_daily_summary", AsyncMock()),
    ):
        await orchestrator.run_eod_tasks(RUN_DATE)

    mock_iv.assert_called_once_with(RUN_DATE)


@pytest.mark.asyncio
async def test_eod_tasks_swallows_iv_builder_exception() -> None:
    from src.fno import orchestrator

    with (
        patch.object(orchestrator._settings, "fno_module_enabled", True),
        patch("src.fno.orchestrator.build_for_date", AsyncMock(side_effect=RuntimeError("DB error"))),
        patch("src.fno.orchestrator._send_daily_summary", AsyncMock()),
    ):
        await orchestrator.run_eod_tasks(RUN_DATE)  # should not raise
