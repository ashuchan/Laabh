"""Coverage for src.fno.llm_features.parse_llm_features.

Pins the v10 parser's contracts: clamping, degenerate-output rejection,
fenced-JSON tolerance, structural field parsing.
"""
from __future__ import annotations

import json
from datetime import date

from src.fno.llm_features import LLMFeatureScore, parse_llm_features


def _well_formed() -> dict:
    return {
        "directional_conviction": 0.4,
        "thesis_durability": 0.6,
        "catalyst_specificity": 0.7,
        "risk_flag": -0.1,
        "raw_confidence": 0.55,
        "proposed_structure": "bull_call_spread",
        "proposed_strikes": [19500, 19700],
        "proposed_expiry": "2026-05-22",
        "reasoning_oneline": "Q4 results May 16, IV rank 78",
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_parse_happy_path_returns_score() -> None:
    r = parse_llm_features(_well_formed())
    assert isinstance(r, LLMFeatureScore)
    assert r.directional_conviction == 0.4
    assert r.thesis_durability == 0.6
    assert r.proposed_strikes == [19500.0, 19700.0]
    assert r.proposed_expiry == date(2026, 5, 22)
    assert r.proposed_structure == "bull_call_spread"


def test_parse_accepts_json_string() -> None:
    r = parse_llm_features(json.dumps(_well_formed()))
    assert r is not None and r.directional_conviction == 0.4


# ---------------------------------------------------------------------------
# Clamping — clip each field to its declared range.
# ---------------------------------------------------------------------------


def test_parse_clamps_conviction_high() -> None:
    payload = _well_formed() | {"directional_conviction": 5.0}
    r = parse_llm_features(payload)
    assert r is not None and r.directional_conviction == 1.0


def test_parse_clamps_conviction_low() -> None:
    payload = _well_formed() | {"directional_conviction": -3.0}
    r = parse_llm_features(payload)
    assert r is not None and r.directional_conviction == -1.0


def test_parse_clamps_durability_negative() -> None:
    payload = _well_formed() | {"thesis_durability": -0.5}
    r = parse_llm_features(payload)
    assert r is not None and r.thesis_durability == 0.0


def test_parse_clamps_specificity_over_one() -> None:
    payload = _well_formed() | {"catalyst_specificity": 2.0}
    r = parse_llm_features(payload)
    assert r is not None and r.catalyst_specificity == 1.0


def test_parse_clamps_risk_flag_below_minus_one() -> None:
    payload = _well_formed() | {"risk_flag": -3.0}
    r = parse_llm_features(payload)
    assert r is not None and r.risk_flag == -1.0


def test_parse_clamps_risk_flag_positive_to_zero() -> None:
    """risk_flag is supposed to be [-1, 0] — positive values clamp at 0."""
    payload = _well_formed() | {"risk_flag": 0.5}
    r = parse_llm_features(payload)
    assert r is not None and r.risk_flag == 0.0


# ---------------------------------------------------------------------------
# Degenerate output — all four numeric fields zero → reject.
# ---------------------------------------------------------------------------


def test_parse_rejects_all_zero_degenerate() -> None:
    payload = {
        "directional_conviction": 0.0,
        "thesis_durability": 0.0,
        "catalyst_specificity": 0.0,
        "risk_flag": 0.0,
        "raw_confidence": 0.0,
    }
    assert parse_llm_features(payload) is None


def test_parse_keeps_near_zero_with_non_zero_durability() -> None:
    """Only ALL FOUR zero rejects — partial zeros are still valid."""
    payload = _well_formed() | {
        "directional_conviction": 0.0,
        "catalyst_specificity": 0.0,
        "risk_flag": 0.0,
        "thesis_durability": 0.5,
    }
    r = parse_llm_features(payload)
    assert r is not None


# ---------------------------------------------------------------------------
# Tolerant parsing — fenced JSON, garbage, missing fields.
# ---------------------------------------------------------------------------


def test_parse_strips_json_code_fence() -> None:
    raw = "```json\n" + json.dumps(_well_formed()) + "\n```"
    r = parse_llm_features(raw)
    assert r is not None and r.directional_conviction == 0.4


def test_parse_strips_bare_code_fence() -> None:
    raw = "```\n" + json.dumps(_well_formed()) + "\n```"
    r = parse_llm_features(raw)
    assert r is not None


def test_parse_extracts_first_json_object_from_prose() -> None:
    raw = (
        "Here is the analysis: "
        + json.dumps(_well_formed())
        + " — let me know if you want more."
    )
    r = parse_llm_features(raw)
    assert r is not None


def test_parse_garbage_returns_none() -> None:
    assert parse_llm_features("nothing here") is None
    assert parse_llm_features("") is None


def test_parse_invalid_expiry_returns_none_for_expiry() -> None:
    """Bad date doesn't reject the whole row — just nulls the expiry."""
    payload = _well_formed() | {"proposed_expiry": "not-a-date"}
    r = parse_llm_features(payload)
    assert r is not None and r.proposed_expiry is None


def test_parse_non_list_strikes_returns_none_for_strikes() -> None:
    payload = _well_formed() | {"proposed_strikes": "not a list"}
    r = parse_llm_features(payload)
    assert r is not None and r.proposed_strikes is None


def test_parse_empty_strikes_normalises_to_none() -> None:
    payload = _well_formed() | {"proposed_strikes": []}
    r = parse_llm_features(payload)
    assert r is not None and r.proposed_strikes is None


def test_parse_truncates_long_reasoning() -> None:
    long = "x" * 500
    payload = _well_formed() | {"reasoning_oneline": long}
    r = parse_llm_features(payload)
    assert r is not None and len(r.reasoning_oneline) <= 240
