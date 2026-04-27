"""Shared pytest fixtures for Laabh tests.

Provides:
  - async_session: a mock SQLAlchemy async session
  - mock_settings: Settings with all test values pre-filled
  - httpx_mock_transport: drop-in transport for httpx without real network calls
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Minimal async session mock
# ---------------------------------------------------------------------------

class _FakeResult:
    """Mimics the result of session.execute(...)."""
    def __init__(self, rows: list = None):
        self._rows = rows or []

    def all(self):
        return self._rows

    def scalars(self):
        return self

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def first(self):
        return self._rows[0] if self._rows else None


class MockAsyncSession:
    """Lightweight async session mock — records add() calls, returns pre-set results."""

    def __init__(self, execute_results: list | None = None):
        self._execute_results = list(execute_results or [])
        self._call_index = 0
        self.added: list = []
        self.flush = AsyncMock()

    async def execute(self, *args, **kwargs):
        if self._call_index < len(self._execute_results):
            result = self._execute_results[self._call_index]
            self._call_index += 1
            if isinstance(result, _FakeResult):
                return result
            return _FakeResult(result if isinstance(result, list) else [result])
        return _FakeResult([])

    def add(self, obj):
        self.added.append(obj)


@pytest.fixture
def fake_result():
    """Factory for _FakeResult."""
    return _FakeResult


@pytest.fixture
def mock_session_factory():
    """Return a factory that creates MockAsyncSession instances."""
    return MockAsyncSession


# ---------------------------------------------------------------------------
# Async session_scope patcher
# ---------------------------------------------------------------------------

@pytest.fixture
def patch_session(monkeypatch):
    """Returns a helper that patches src.db.session_scope with a given session."""

    def _patch(session: MockAsyncSession):
        @asynccontextmanager
        async def _scope():
            yield session

        monkeypatch.setattr("src.db.session_scope", _scope)
        return session

    return _patch


# ---------------------------------------------------------------------------
# Settings fixture (no env file needed)
# ---------------------------------------------------------------------------

@pytest.fixture
def test_settings():
    """Return a Settings-like object with safe test defaults."""
    settings = MagicMock()
    settings.fno_module_enabled = True
    settings.fno_phase1_min_atm_oi = 50_000
    settings.fno_phase1_max_atm_spread_pct = 0.005
    settings.fno_phase1_min_avg_volume_5d = 10_000
    settings.fno_phase2_min_composite_score = 10.0
    settings.fno_phase2_news_lookback_hours = 18
    settings.fno_phase2_weight_news = 1.0
    settings.fno_phase2_weight_sentiment = 1.0
    settings.fno_phase2_weight_fii_dii = 0.8
    settings.fno_phase2_weight_macro = 0.8
    settings.fno_phase2_weight_convergence = 1.5
    settings.fno_phase3_target_output = 10
    settings.fno_phase3_llm_model = "claude-sonnet-4-20250514"
    settings.fno_phase3_llm_temperature = 0.0
    settings.fno_ranker_version = "v1"
    settings.fno_vix_low_threshold = 12.0
    settings.fno_vix_high_threshold = 18.0
    settings.telegram_bot_token = ""
    settings.telegram_chat_id = ""
    settings.anthropic_api_key = "test-key"
    return settings


# ---------------------------------------------------------------------------
# Common test data factories
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_instrument_id():
    return uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def sample_run_date():
    return date(2026, 4, 27)


@pytest.fixture
def sample_candidate_row(sample_instrument_id, sample_run_date):
    """A minimal FNOCandidate-like object."""
    obj = MagicMock()
    obj.id = uuid.uuid4()
    obj.instrument_id = sample_instrument_id
    obj.run_date = sample_run_date
    obj.phase = 2
    obj.passed_liquidity = True
    obj.composite_score = Decimal("7.5")
    obj.news_score = Decimal("7.0")
    obj.sentiment_score = Decimal("6.0")
    obj.fii_dii_score = Decimal("8.0")
    obj.macro_align_score = Decimal("7.0")
    obj.convergence_score = Decimal("7.5")
    obj.iv_rank_52w = Decimal("35.0")
    return obj
