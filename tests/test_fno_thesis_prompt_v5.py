"""Lock-in tests for the v5 F&O thesis prompt (relaxation of SKIP-bias).

Two changes versus v4 — see ``src.fno.prompts`` module docstring for full
context:
  1. REGIME GATE no longer prescribes ``return decision='SKIP'`` as the
     escape when IV is high. It now mandates a structural pivot (debit
     spread / iron_condor / short_strangle) so the LLM can still PROCEED
     with an appropriate vehicle.
  2. A new "DECISION BIAS" section reserves SKIP for genuinely
     untradeable instruments and steers uncertain-but-tradeable setups
     toward HEDGE.

These tests are guardrails against accidental regression to v4 wording
or schema. They intentionally do NOT call Claude — the live LLM behavior
is verified by the dry-run script ``scripts/fno_phase3_dry_run.py``
(invoked manually after a prompt change, not in CI).
"""
from __future__ import annotations

import json
import re

from src.fno.prompts import (
    FNO_THESIS_PROMPT_VERSION,
    FNO_THESIS_SYSTEM,
    FNO_THESIS_USER_TEMPLATE,
)
from src.fno.thesis_synthesizer import parse_llm_response


# ---------------------------------------------------------------------------
# Version + identity
# ---------------------------------------------------------------------------

def test_prompt_version_is_v5():
    """A bumped version is the user-visible signal that the prompt changed.

    Audit-log rows in ``llm_audit_log`` capture the model + temperature
    but not the prompt body; the version field is how downstream
    consumers (and humans) can tell which decision logic applied to a
    given run.
    """
    assert FNO_THESIS_PROMPT_VERSION == "v5"


# ---------------------------------------------------------------------------
# REGIME GATE rewrite — the heart of the relaxation
# ---------------------------------------------------------------------------

def test_regime_gate_no_longer_prescribes_skip_as_escape():
    """The v4 prompt told the LLM 'If only naked-long is viable for the
    thesis, return decision="SKIP"' inside the REGIME GATE rule. That
    instruction is what caused 0 PROCEEDs on every high-IV day after
    iv_history wiring (05-06 onward). v5 removes it.

    We check the *exact* deletion: the phrase ``"return decision='SKIP'"``
    appears nowhere inside the REGIME GATE rule's text. (It may legitimately
    appear elsewhere — e.g. in MARKET MOVERS or THESIS DURABILITY.)"""
    # Slice out the REGIME GATE rule (rule 1) — bounded by the next rule
    # number. This isolates the rewrite zone from the rest of the prompt
    # so we don't accidentally pass when 'SKIP' still appears in rule 1
    # via a comment / leftover.
    match = re.search(
        r"1\. REGIME GATE:.*?(?=^\d+\. )",
        FNO_THESIS_SYSTEM,
        re.DOTALL | re.MULTILINE,
    )
    assert match is not None, "REGIME GATE rule should still be rule #1"
    regime_block = match.group(0)
    # The forbidden v4 escape clause must be gone.
    assert "return decision='SKIP'" not in regime_block
    assert "return decision=\"SKIP\"" not in regime_block
    # And the new pivot language must be present
    assert "pivot" in regime_block.lower() or "debit spread" in regime_block.lower()


def test_regime_gate_prescribes_structural_pivots():
    """The new REGIME GATE must name the three replacement structures so
    the LLM has a concrete picklist instead of defaulting to SKIP."""
    # Locate the rule again
    match = re.search(
        r"1\. REGIME GATE:.*?(?=^\d+\. )",
        FNO_THESIS_SYSTEM,
        re.DOTALL | re.MULTILINE,
    )
    regime_block = match.group(0)
    # Debit-spread family (directional pivot)
    assert "bull_call_spread" in regime_block
    assert "bear_put_spread" in regime_block
    # Credit / range family (neutral pivot)
    assert "iron_condor" in regime_block
    assert "short_strangle" in regime_block


def test_regime_gate_allows_skip_only_for_unfavorable_structure_ev():
    """The remaining SKIP escape (so the LLM isn't trapped when IV is
    pathologically skewed) must be guarded by an EV check, not just
    'IV is high'."""
    match = re.search(
        r"1\. REGIME GATE:.*?(?=^\d+\. )",
        FNO_THESIS_SYSTEM,
        re.DOTALL | re.MULTILINE,
    )
    regime_block = match.group(0).lower()
    # The new escape must mention "unfavorable expected value" or equivalent
    # and explicitly require that the LLM cites the structural problem,
    # not the high-IV regime itself.
    assert "unfavorable expected value" in regime_block or "uneconomic" in regime_block


# ---------------------------------------------------------------------------
# Decision-bias section — pushes uncertain trades toward HEDGE
# ---------------------------------------------------------------------------

def test_decision_bias_section_present():
    """The new DECISION BIAS block exists and explicitly names HEDGE as
    the preferred verdict for uncertain-but-tradeable setups."""
    assert "DECISION BIAS" in FNO_THESIS_SYSTEM
    # The section must guide HEDGE for "at least one catalyst score ≥ 6"
    # — that's the threshold we use elsewhere in catalyst_scorer for a
    # meaningful single-factor signal.
    bias_block = FNO_THESIS_SYSTEM.split("DECISION BIAS")[1]
    assert "HEDGE" in bias_block
    # SKIP must still be a documented option but explicitly scoped to
    # genuinely untradeable cases
    assert "SKIP" in bias_block
    assert "no actionable structure" in bias_block.lower() or "nothing to trade" in bias_block.lower()


def test_decision_bias_clarifies_high_iv_does_not_block_proceed():
    """Defensive against a future re-introduction of the v4 bug: the bias
    section explicitly says high-IV regime constrains the *structure*,
    not the *decision*."""
    bias_block = FNO_THESIS_SYSTEM.split("DECISION BIAS")[1]
    # Either phrasing is acceptable as long as the intent is clear
    text = bias_block.lower()
    assert (
        "regime does not block proceed" in text
        or "high-iv regime does not block" in text
        or ("constrains the structure" in text and "not the" in text)
    )


# ---------------------------------------------------------------------------
# Schema invariants — parse_llm_response must still work
# ---------------------------------------------------------------------------

def test_system_prompt_still_asks_for_json_schema():
    """Schema didn't change in v5 — must still ask for the same JSON shape."""
    for required in ("decision", "direction", "thesis", "risk_factors", "confidence"):
        assert required in FNO_THESIS_SYSTEM, f"system prompt missing schema key: {required}"
    # The three legal decision values
    assert "PROCEED" in FNO_THESIS_SYSTEM
    assert "SKIP" in FNO_THESIS_SYSTEM
    assert "HEDGE" in FNO_THESIS_SYSTEM


def test_parse_llm_response_unchanged_for_canonical_v5_response():
    """A response shaped like the v5 schema must round-trip through the
    existing parser — confirms schema compatibility."""
    raw = json.dumps({
        "decision": "PROCEED",
        "direction": "bullish",
        "thesis": "Pivoted to bull_call_spread given high IV regime.",
        "risk_factors": ["earnings risk", "broker downgrade"],
        "confidence": 0.62,
    })
    parsed = parse_llm_response(raw)
    assert parsed["decision"] == "PROCEED"
    assert parsed["direction"] == "bullish"
    assert parsed["confidence"] == 0.62


def test_user_template_unchanged_placeholders():
    """The user template's format placeholders are the contract Phase 3
    code fills in via build_user_prompt. v5 only changes the system
    prompt — the user template is untouched."""
    required_placeholders = [
        "{symbol}", "{sector}", "{underlying_price}",
        "{iv_rank_block}", "{iv_regime}", "{oi_structure}",
        "{days_to_expiry}",
        "{news_score}", "{bullish_count}", "{bearish_count}",
        "{lookback_hours}", "{sentiment_score}", "{fii_dii_block}",
        "{macro_align_score}", "{macro_drivers}",
        "{convergence_score}", "{composite_score}",
        "{headlines}", "{market_movers_context}", "{extra_context}",
    ]
    for ph in required_placeholders:
        assert ph in FNO_THESIS_USER_TEMPLATE, f"user template missing placeholder: {ph}"
