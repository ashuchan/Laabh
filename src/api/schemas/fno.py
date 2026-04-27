"""Pydantic response schemas for F&O API endpoints."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict


class FNOCandidateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    instrument_id: uuid.UUID
    symbol: str | None = None
    run_date: date
    phase: int
    passed_liquidity: bool | None = None
    atm_oi: int | None = None
    atm_spread_pct: Decimal | None = None
    avg_volume_5d: int | None = None
    news_score: Decimal | None = None
    sentiment_score: Decimal | None = None
    fii_dii_score: Decimal | None = None
    macro_align_score: Decimal | None = None
    convergence_score: Decimal | None = None
    composite_score: Decimal | None = None
    technical_pass: bool | None = None
    iv_regime: str | None = None
    oi_structure: str | None = None
    llm_thesis: str | None = None
    llm_decision: str | None = None
    config_version: str | None = None
    created_at: datetime


class IVHistoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    instrument_id: uuid.UUID
    date: date
    atm_iv: Decimal
    iv_rank_52w: Decimal | None = None
    iv_percentile_52w: Decimal | None = None


class VIXTickResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    timestamp: datetime
    vix_value: Decimal
    regime: Literal["low", "neutral", "high"]


class FNOBanListResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    symbol: str
    ban_date: date
    is_active: bool


class PipelineTriggerResponse(BaseModel):
    status: str
    run_date: str
    phase1_passed: int = 0
    phase2_passed: int = 0
    phase3_proceed: int = 0


class ChainIssueResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: str
    instrument_id: uuid.UUID | None = None
    issue_type: str
    error_message: str
    raw_response: str | None = None
    detected_at: datetime | None = None
    github_issue_url: str | None = None
    resolved_at: datetime | None = None
    resolved_by: str | None = None


class ResolveIssueResponse(BaseModel):
    id: uuid.UUID
    resolved: bool
    source_health_status: str


class SourceHealthResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source: str
    status: str
    consecutive_errors: int
    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None
    updated_at: datetime | None = None
