"""Tests for src/eval/shadow.py — truncation, alert logic, score fetching."""
from __future__ import annotations

import json
import pytest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

from src.eval.shadow import (
    _truncate_for_eval,
    check_daily_eval_alerts,
    fetch_recent_overall_scores,
)


# ---------------------------------------------------------------------------
# _truncate_for_eval
# ---------------------------------------------------------------------------

class TestTruncateForEval:
    def test_small_dict_returned_unchanged(self):
        data = {"triage": {"skip_today": False}, "judge": {"decision": "BUY"}}
        result = _truncate_for_eval(data)
        assert result["triage"]["skip_today"] is False
        assert result["judge"]["decision"] == "BUY"

    def test_explorer_list_trimmed_to_first_element(self):
        data = {
            "explorer_BANKNIFTY": [{"tldr": "bullish"}, {"tldr": "bearish"}, {"tldr": "neutral"}]
        }
        result = _truncate_for_eval(data)
        assert len(result["explorer_BANKNIFTY"]) == 1
        assert result["explorer_BANKNIFTY"][0]["tldr"] == "bullish"

    def test_long_string_values_trimmed(self):
        long_val = "x" * 5_000
        data = {"agent_note": long_val}
        result = _truncate_for_eval(data)
        assert len(result["agent_note"]) < len(long_val)
        assert result["agent_note"].endswith("…[trimmed]")

    def test_nested_long_strings_trimmed(self):
        data = {"judge": {"decision_summary": "a" * 3_000}}
        result = _truncate_for_eval(data)
        assert len(result["judge"]["decision_summary"]) <= 2_010  # 2000 + "…[trimmed]"

    def test_oversized_blob_returns_truncated_marker(self):
        # build a dict whose serialisation exceeds the max_chars limit
        huge = {"key": "v" * 60_000}
        result = _truncate_for_eval(huge, max_chars=100)
        assert "_truncated_blob" in result
        assert "_truncation_note" in result

    def test_non_explorer_lists_not_trimmed(self):
        data = {"predictions": [{"a": 1}, {"b": 2}, {"c": 3}]}
        result = _truncate_for_eval(data)
        assert len(result["predictions"]) == 3

    def test_does_not_mutate_input(self):
        original = {"explorer_X": [{"a": 1}, {"b": 2}], "other": "val"}
        _ = _truncate_for_eval(original)
        assert len(original["explorer_X"]) == 2  # original unmodified


# ---------------------------------------------------------------------------
# fetch_recent_overall_scores
# ---------------------------------------------------------------------------

def _make_db_factory(rows: list):
    @asynccontextmanager
    async def factory():
        db = AsyncMock()
        mock_result = MagicMock()
        mock_result.fetchall.return_value = rows
        db.execute = AsyncMock(return_value=mock_result)
        yield db

    return factory


class TestFetchRecentOverallScores:
    @pytest.mark.asyncio
    async def test_returns_floats_for_valid_rows(self):
        factory = _make_db_factory([(7.5,), (8.0,), (6.0,)])
        scores = await fetch_recent_overall_scores(factory, days=3)
        assert scores == [7.5, 8.0, 6.0]

    @pytest.mark.asyncio
    async def test_skips_none_rows(self):
        factory = _make_db_factory([(7.5,), (None,), (6.0,)])
        scores = await fetch_recent_overall_scores(factory, days=3)
        assert None not in scores
        assert len(scores) == 2

    @pytest.mark.asyncio
    async def test_returns_empty_on_db_error(self):
        @asynccontextmanager
        async def bad_factory():
            raise RuntimeError("DB down")
            yield  # unreachable; satisfies async generator syntax

        scores = await fetch_recent_overall_scores(bad_factory, days=3)
        assert scores == []


# ---------------------------------------------------------------------------
# check_daily_eval_alerts
# ---------------------------------------------------------------------------

class TestCheckDailyEvalAlerts:
    @pytest.mark.asyncio
    async def test_no_alert_when_scores_above_threshold(self):
        factory = _make_db_factory([(8.0,), (7.5,), (9.0,)])
        alerted = await check_daily_eval_alerts(factory, telegram=None)
        assert alerted is False

    @pytest.mark.asyncio
    async def test_alert_fired_when_avg_below_threshold(self):
        factory = _make_db_factory([(4.0,), (5.0,), (4.5,)])
        telegram = AsyncMock()
        telegram.send = AsyncMock()
        alerted = await check_daily_eval_alerts(
            factory, telegram=telegram, chat_id="chat123"
        )
        assert alerted is True
        telegram.send.assert_awaited_once()
        call_kwargs = telegram.send.call_args.kwargs
        assert call_kwargs["chat_id"] == "chat123"
        assert "degradation" in call_kwargs["text"].lower()

    @pytest.mark.asyncio
    async def test_no_alert_when_no_scores(self):
        factory = _make_db_factory([])
        alerted = await check_daily_eval_alerts(factory, telegram=None)
        assert alerted is False

    @pytest.mark.asyncio
    async def test_telegram_failure_does_not_raise(self):
        factory = _make_db_factory([(3.0,), (3.0,), (3.0,)])
        telegram = AsyncMock()
        telegram.send = AsyncMock(side_effect=RuntimeError("network error"))
        # Should not raise even if telegram fails
        alerted = await check_daily_eval_alerts(
            factory, telegram=telegram, chat_id="chat123"
        )
        assert alerted is True

    @pytest.mark.asyncio
    async def test_exactly_at_threshold_does_not_alert(self):
        # avg == threshold should NOT fire (only below fires)
        factory = _make_db_factory([(6.0,), (6.0,), (6.0,)])
        alerted = await check_daily_eval_alerts(
            factory, telegram=None, alert_threshold=6.0
        )
        assert alerted is False
