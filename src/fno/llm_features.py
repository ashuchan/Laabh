"""LLMFeatureScore — parser + validator for the v10 continuous prompt.

Plan reference: docs/llm_feature_generator/implementation_plan.md §1.2.

The v10 prompt asks Claude for four continuous scores plus structured
proposed strikes/expiry. This module parses the response, clamps each
field to its declared range, and rejects degenerate outputs (all four
numeric fields zero — usually a sign the model lazily defaulted).

Callers should treat ``parse_llm_features`` returning None as "fall back
to v9 categorical with a warning logged" — never as a silent skip.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class LLMFeatureScore:
    """v10 continuous-feature payload, post-clamp, post-validation."""

    directional_conviction: float           # clipped [-1, 1]
    thesis_durability: float                 # clipped [0, 1]
    catalyst_specificity: float              # clipped [0, 1]
    risk_flag: float                         # clipped [-1, 0]
    raw_confidence: float                    # clipped [0, 1]
    proposed_structure: str | None
    proposed_strikes: list[float] | None
    proposed_expiry: date | None
    reasoning_oneline: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def parse_llm_features(
    raw_response: str | dict,
    *,
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> LLMFeatureScore | None:
    """Parse a v10 raw LLM response. Returns None on degenerate output.

    ``as_of`` and ``dryrun_run_id`` are accepted per CLAUDE.md pipeline
    convention — unused inside but reserved for future audit-routing.
    """
    payload = _coerce_to_dict(raw_response)
    if payload is None:
        return None

    dc = _clip(payload.get("directional_conviction"), -1.0, 1.0)
    td = _clip(payload.get("thesis_durability"), 0.0, 1.0)
    cs = _clip(payload.get("catalyst_specificity"), 0.0, 1.0)
    rf = _clip(payload.get("risk_flag"), -1.0, 0.0)
    rc = _clip(payload.get("raw_confidence"), 0.0, 1.0)

    # Degenerate guard: all four feature dimensions exactly zero is almost
    # always a lazy-default output. The bandit can't learn from these and
    # the calibration model fits on noise. Reject — caller falls back to v9.
    if dc == 0.0 and td == 0.0 and cs == 0.0 and rf == 0.0:
        return None

    structure = payload.get("proposed_structure")
    if isinstance(structure, str):
        structure = structure.strip().lower() or None
    else:
        structure = None

    strikes_raw = payload.get("proposed_strikes")
    strikes: list[float] | None
    if isinstance(strikes_raw, list) and strikes_raw:
        try:
            strikes = [float(s) for s in strikes_raw if s is not None]
            strikes = strikes or None
        except (TypeError, ValueError):
            strikes = None
    else:
        strikes = None

    expiry: date | None
    expiry_raw = payload.get("proposed_expiry")
    if isinstance(expiry_raw, str):
        expiry = _safe_iso_date(expiry_raw)
    else:
        expiry = None

    reasoning = str(payload.get("reasoning_oneline") or "")[:240]

    return LLMFeatureScore(
        directional_conviction=dc,
        thesis_durability=td,
        catalyst_specificity=cs,
        risk_flag=rf,
        raw_confidence=rc,
        proposed_structure=structure,
        proposed_strikes=strikes,
        proposed_expiry=expiry,
        reasoning_oneline=reasoning,
        raw=payload,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_to_dict(raw: str | dict) -> dict[str, Any] | None:
    """Tolerate ``json.loads``-ready strings, fenced JSON, or already-parsed dicts."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None

    stripped = raw.strip()
    if not stripped:
        return None

    # 1. Plain JSON.
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # 2. Strip ```json ... ``` fence.
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*\n?", "", stripped, count=1)
        stripped = re.sub(r"\n?```\s*$", "", stripped, count=1)
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

    # 3. Largest balanced-looking {…} substring.
    m = re.search(r"\{.*\}", stripped, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _clip(value: Any, lo: float, hi: float) -> float:
    """Coerce to float, clamp to [lo, hi]; missing/non-numeric → lo if lo>=0 else 0."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0 if lo <= 0.0 <= hi else lo
    if f != f:   # NaN
        return 0.0 if lo <= 0.0 <= hi else lo
    return max(lo, min(hi, f))


def _safe_iso_date(s: str) -> date | None:
    try:
        return date.fromisoformat(s.strip())
    except (TypeError, ValueError):
        return None
