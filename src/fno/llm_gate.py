"""LLM gate bypass — Phase 3 cutover helper.

Plan reference: docs/llm_feature_generator/implementation_plan.md §3.2.

Under ``LAABH_LLM_MODE='feature'`` the categorical PROCEED filter is removed
from every downstream consumer: all Phase-2 passers proceed to the bandit,
which decides per-tick which arm to play using the LLM-augmented context.
Under ``'gate'`` (default) and ``'shadow'`` the legacy behaviour stands.

The helper returns SQLAlchemy where-clauses appropriate to the active mode.
Call sites use it as ``.where(*phase3_gate_filters())``.
"""
from __future__ import annotations

from src.config import get_settings
from src.models.fno_candidate import FNOCandidate


def phase3_gate_filters() -> tuple:
    """Return the SQLAlchemy where-clauses for downstream PROCEED filtering.

    Empty tuple when mode='feature' (gate removed); the categorical
    predicate otherwise.
    """
    if get_settings().laabh_llm_mode == "feature":
        return ()
    return (FNOCandidate.llm_decision == "PROCEED",)
