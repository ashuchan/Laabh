"""Unit tests for the equity + F&O strategy gate.

Covers every named violation code and the high-VIX confidence-before-count
ordering rule that landed in the post-review fixes.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal

import pytest

from src.trading.strategy_gate import (
    FNOProposalView,
    GateOutcome,
    HIGH_VIX_MAX_NEW_ENTRIES,
    SUB_SCALE_CASH_THRESHOLD,
    filter_equity_actions,
    filter_fno_proposals,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _patch_db(monkeypatch):
    """Stub out the two DB-touching helpers in strategy_gate so tests stay
    pure-Python. Each test can override via monkeypatch where needed.
    """
    async def _no_holdings(_pid):
        return set()

    async def _no_open_book():
        return {"same": set(), "directions": {}}

    monkeypatch.setattr(
        "src.trading.strategy_gate._held_instrument_ids", _no_holdings
    )
    monkeypatch.setattr(
        "src.trading.strategy_gate._open_fno_book_index", _no_open_book
    )


def _action(
    iid: str,
    *,
    action: str = "BUY",
    asset_class: str = "EQUITY",
    approx_price: float = 100.0,
    qty: int = 10,
    reason: str = "",
) -> dict:
    return {
        "instrument_id": iid,
        "asset_class": asset_class,
        "action": action,
        "approx_price": approx_price,
        "qty": qty,
        "reason": reason or iid,
    }


def _snap(
    *,
    cash: float = 40_000.0,
    vix: float = 18.46,
    regime: str = "high",
    candidates: list[dict] | None = None,
) -> dict:
    return {
        "cash_available": cash,
        "market": {"vix_value": vix, "vix_regime": regime},
        "candidates": candidates or [],
    }


# ---------------------------------------------------------------------------
# Equity gate — Rule A (sub-scale)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sub_scale_low_confidence_rejected():
    """Sub-scale capital + confidence < 0.75 → A_sub_scale_confidence."""
    snap = _snap(
        cash=40_000.0,
        candidates=[
            {"instrument_id": "a1", "confidence": 0.65, "ltp": 100.0,
             "target_price": 105.0},
        ],
    )
    out = await filter_equity_actions([_action("a1")], snapshot=snap)
    assert len(out.skipped) == 1
    assert out.skipped[0]["gate_violation"] == "A_sub_scale_confidence"


@pytest.mark.asyncio
async def test_sub_scale_small_move_rejected():
    """Sub-scale + expected move < 2% → A_sub_scale_move."""
    snap = _snap(
        cash=40_000.0,
        regime="neutral",
        vix=14.0,
        candidates=[
            {"instrument_id": "a1", "confidence": 0.85, "ltp": 100.0,
             "target_price": 101.5},
        ],
    )
    out = await filter_equity_actions([_action("a1")], snapshot=snap)
    assert len(out.skipped) == 1
    assert out.skipped[0]["gate_violation"] == "A_sub_scale_move"


@pytest.mark.asyncio
async def test_above_scale_skips_rule_a():
    """Cash >= threshold means Rule A doesn't fire."""
    snap = _snap(
        cash=SUB_SCALE_CASH_THRESHOLD + 1,
        regime="neutral",
        vix=14.0,
        candidates=[
            {"instrument_id": "a1", "confidence": 0.65, "ltp": 100.0,
             "target_price": 101.0},
        ],
    )
    out = await filter_equity_actions([_action("a1")], snapshot=snap)
    assert len(out.accepted) == 1
    assert out.skipped == []


# ---------------------------------------------------------------------------
# Equity gate — Rule B (high-VIX) and the confidence-before-count fix
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_high_vix_low_confidence_rejected_first():
    """A high-VIX entry with confidence < 0.75 must be filtered BEFORE the
    count cap so it never burns a slot from a high-confidence entry.
    """
    # Three eligible BUYs, all > 200K cash (so Rule A doesn't fire).
    snap = _snap(
        cash=300_000.0,
        candidates=[
            {"instrument_id": "low", "confidence": 0.70, "ltp": 100.0,
             "target_price": 110.0},   # rejected on confidence
            {"instrument_id": "mid", "confidence": 0.78, "ltp": 100.0,
             "target_price": 110.0},   # accepted (slot 2 by confidence)
            {"instrument_id": "top", "confidence": 0.92, "ltp": 100.0,
             "target_price": 110.0},   # accepted (slot 1 by confidence)
        ],
    )
    actions = [
        _action("low"),  # listed first — would have won the count race
        _action("mid"),
        _action("top"),
    ]
    out = await filter_equity_actions(actions, snapshot=snap)

    accepted_iids = {a["instrument_id"] for a in out.accepted}
    skipped_iids = {a["instrument_id"]: a["gate_violation"]
                    for a in out.skipped}
    assert accepted_iids == {"mid", "top"}
    assert skipped_iids == {"low": "B_high_vix_confidence"}


@pytest.mark.asyncio
async def test_high_vix_count_cap_picks_highest_confidence():
    """When all eligible BUYs clear the confidence floor, the count cap
    keeps the top-N by confidence.
    """
    snap = _snap(
        cash=300_000.0,
        candidates=[
            {"instrument_id": "a", "confidence": 0.82, "ltp": 100.0,
             "target_price": 110.0},
            {"instrument_id": "b", "confidence": 0.86, "ltp": 100.0,
             "target_price": 110.0},
            {"instrument_id": "c", "confidence": 0.79, "ltp": 100.0,
             "target_price": 110.0},
            {"instrument_id": "d", "confidence": 0.91, "ltp": 100.0,
             "target_price": 110.0},
        ],
    )
    actions = [_action(x) for x in ("a", "b", "c", "d")]
    out = await filter_equity_actions(actions, snapshot=snap)

    accepted_iids = {a["instrument_id"] for a in out.accepted}
    assert len(out.accepted) == HIGH_VIX_MAX_NEW_ENTRIES
    assert accepted_iids == {"b", "d"}  # highest two by confidence
    assert all(a["gate_violation"] == "B_high_vix_count" for a in out.skipped)


# ---------------------------------------------------------------------------
# Equity gate — Rule D (portfolio-aware)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_buy_already_held_rejected(monkeypatch):
    """BUY for an instrument already in holdings → D_already_held."""
    async def _held(_pid):
        return {"a1"}
    monkeypatch.setattr(
        "src.trading.strategy_gate._held_instrument_ids", _held
    )
    snap = _snap(cash=300_000.0, regime="neutral", vix=14.0)
    out = await filter_equity_actions(
        [_action("a1")], snapshot=snap, portfolio_id="x"
    )
    assert out.skipped[0]["gate_violation"] == "D_already_held"


@pytest.mark.asyncio
async def test_sell_without_holding_rejected():
    """SELL for an instrument not in holdings → D_sell_without_holding."""
    snap = _snap(cash=300_000.0, regime="neutral", vix=14.0)
    out = await filter_equity_actions(
        [_action("a1", action="SELL")], snapshot=snap, portfolio_id="x"
    )
    assert out.skipped[0]["gate_violation"] == "D_sell_without_holding"


# ---------------------------------------------------------------------------
# F&O gate — Rule F1 (regime), F2 (stops), F3a/F3b (book-aware)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_high_vix_blocks_naked_long_call():
    """VIX >= 17 + long_call → F1_high_vix_naked_long."""
    p = FNOProposalView(
        instrument_id="i1", symbol="X", expiry_date="2026-05-08",
        strategy_name="long_call",
        entry_premium=Decimal("1000"), stop_premium=Decimal("700"),
    )
    accepted, rejected = await filter_fno_proposals([p], vix_value=18.5)
    assert accepted == []
    assert rejected[0][1] == "F1_high_vix_naked_long"


@pytest.mark.asyncio
async def test_high_vix_allows_debit_spread():
    """Debit spread is allowed in high-VIX (defined risk, not naked long)."""
    p = FNOProposalView(
        instrument_id="i1", symbol="X", expiry_date="2026-05-08",
        strategy_name="bull_call_spread",
        entry_premium=Decimal("500"), stop_premium=Decimal("300"),
    )
    accepted, rejected = await filter_fno_proposals([p], vix_value=18.5)
    assert len(accepted) == 1 and rejected == []


@pytest.mark.asyncio
async def test_iv_regime_fallback_when_vix_missing():
    """Per-proposal iv_regime='high' triggers F1 even when vix_value=None."""
    p = FNOProposalView(
        instrument_id="i1", symbol="X", expiry_date="2026-05-08",
        strategy_name="long_call",
        entry_premium=Decimal("1000"), stop_premium=Decimal("700"),
        iv_regime="high",
    )
    accepted, rejected = await filter_fno_proposals(
        [p], vix_value=None, iv_regime=None
    )
    assert accepted == []
    assert rejected[0][1] == "F1_high_vix_naked_long"


@pytest.mark.asyncio
async def test_excessive_stop_drawdown_rejected():
    """Stop > 45% premium drawdown → F2_stop_drawdown_exceeded."""
    p = FNOProposalView(
        instrument_id="i1", symbol="X", expiry_date="2026-05-08",
        strategy_name="bull_call_spread",  # avoid F1
        entry_premium=Decimal("29309"), stop_premium=Decimal("41"),  # 99.86%
    )
    accepted, rejected = await filter_fno_proposals([p], vix_value=14.0)
    assert accepted == []
    assert rejected[0][1] == "F2_stop_drawdown_exceeded"


@pytest.mark.asyncio
async def test_stop_at_threshold_accepted():
    """Stop at exactly 45% drawdown is allowed (boundary)."""
    p = FNOProposalView(
        instrument_id="i1", symbol="X", expiry_date="2026-05-08",
        strategy_name="bull_call_spread",
        entry_premium=Decimal("1000"), stop_premium=Decimal("550"),  # 45%
    )
    accepted, rejected = await filter_fno_proposals([p], vix_value=14.0)
    assert len(accepted) == 1


@pytest.mark.asyncio
async def test_duplicate_strategy_blocked(monkeypatch):
    """Same strategy already open on same underlying+expiry → F3a."""
    async def _book():
        return {
            "same": {("i1", "2026-05-08", "long_call")},
            "directions": {("i1", "2026-05-08"): {"bullish"}},
        }
    monkeypatch.setattr(
        "src.trading.strategy_gate._open_fno_book_index", _book
    )
    p = FNOProposalView(
        instrument_id="i1", symbol="X", expiry_date="2026-05-08",
        strategy_name="long_call",
        entry_premium=Decimal("1000"), stop_premium=Decimal("700"),
    )
    accepted, rejected = await filter_fno_proposals([p], vix_value=14.0)
    assert accepted == []
    assert rejected[0][1] == "F3a_duplicate_strategy"


@pytest.mark.asyncio
async def test_opposing_direction_blocked(monkeypatch):
    """long_put open + new long_call on same underlying → F3b_opposing_direction."""
    async def _book():
        return {
            "same": {("i1", "2026-05-08", "long_put")},
            "directions": {("i1", "2026-05-08"): {"bearish"}},
        }
    monkeypatch.setattr(
        "src.trading.strategy_gate._open_fno_book_index", _book
    )
    p = FNOProposalView(
        instrument_id="i1", symbol="X", expiry_date="2026-05-08",
        strategy_name="long_call",
        entry_premium=Decimal("1000"), stop_premium=Decimal("700"),
    )
    accepted, rejected = await filter_fno_proposals([p], vix_value=14.0)
    assert accepted == []
    assert rejected[0][1] == "F3b_opposing_direction"


# ---------------------------------------------------------------------------
# GateOutcome.merge_into_actions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_merge_into_actions_tags_violators_as_hold():
    """Violators are returned as HOLD with a [gate] reason prefix so the
    runner sees them but does not execute them."""
    snap = _snap(
        cash=40_000.0,
        candidates=[
            {"instrument_id": "a1", "confidence": 0.65, "ltp": 100.0,
             "target_price": 105.0},
        ],
    )
    out = await filter_equity_actions([_action("a1")], snapshot=snap)
    merged = out.merge_into_actions()
    assert len(merged) == 1
    assert merged[0]["action"] == "HOLD"
    assert merged[0]["reason"].startswith("[gate]")


# ---------------------------------------------------------------------------
# Empty / no-op safety
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_actions_returns_empty():
    out = await filter_equity_actions([], snapshot=_snap())
    assert out.accepted == [] and out.skipped == []


@pytest.mark.asyncio
async def test_non_equity_actions_pass_through():
    """FNO actions in the equity action list aren't gate-checked here —
    the F&O gate is a separate path before entry execution."""
    snap = _snap(cash=40_000.0)
    fno_action = _action("x", asset_class="FNO")
    out = await filter_equity_actions([fno_action], snapshot=snap)
    assert len(out.accepted) == 1
