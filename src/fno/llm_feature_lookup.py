"""Runtime LLM-feature reader — Phase 3 cutover.

Plan reference: docs/llm_feature_generator/implementation_plan.md §3.1.

At each tick the quant bandit needs the *latest* calibrated continuous LLM
features for the underlying it's evaluating. Source of truth is
``llm_decision_log``; the row written by ``_log_v10_shadow`` carries the
raw scores, and Phase 2 fills in ``calibrated_conviction``.

Returns a neutral (all-zero) feature tuple when no row exists for the
underlying on the current run_date — the bandit treats this as "no LLM
information", which is the right behaviour both during warmup and for any
underlying the Phase-3 LLM did not score today.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

from loguru import logger
from sqlalchemy import text

from src.db import session_scope


@dataclass(frozen=True)
class LatestLLMFeatures:
    """Calibrated + raw LLM features pulled at decision time."""

    log_id: int | None
    calibrated_conviction: float
    thesis_durability: float
    catalyst_specificity: float
    risk_flag: float
    is_present: bool   # False when no row was found

    @classmethod
    def neutral(cls) -> "LatestLLMFeatures":
        return cls(
            log_id=None,
            calibrated_conviction=0.0,
            thesis_durability=0.0,
            catalyst_specificity=0.0,
            risk_flag=0.0,
            is_present=False,
        )


async def get_latest_features(
    instrument_id: uuid.UUID | str,
    run_date: date,
    *,
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
    prompt_version: str = "v10_continuous",
) -> LatestLLMFeatures:
    """Return the latest calibrated LLM-feature row for the underlying.

    Selection rule: most-recent v10 row for (run_date, instrument_id).
    When ``calibrated_conviction`` is NULL (Phase 2 hasn't run yet, or no
    active calibration model), the raw ``directional_conviction`` is used
    as a transparent passthrough — the bandit's own learning compensates
    for the missing calibration step.
    """
    sql = text("""
        SELECT id, calibrated_conviction, directional_conviction,
               thesis_durability, catalyst_specificity, risk_flag
        FROM llm_decision_log
        WHERE instrument_id  = :inst
          AND run_date       = :rd
          AND phase          = 'fno_thesis'
          AND prompt_version = :pv
          AND ((:dryrun_run_id IS NULL  AND dryrun_run_id IS NULL)
            OR  dryrun_run_id = :dryrun_run_id)
        ORDER BY as_of DESC
        LIMIT 1
    """)
    async with session_scope() as session:
        row = (await session.execute(sql, {
            "inst": str(instrument_id),
            "rd": run_date,
            "pv": prompt_version,
            "dryrun_run_id": str(dryrun_run_id) if dryrun_run_id else None,
        })).first()

    if row is None:
        return LatestLLMFeatures.neutral()

    # Prefer calibrated when available; fall back to raw directional_conviction.
    calibrated = (
        float(row.calibrated_conviction)
        if row.calibrated_conviction is not None
        else float(row.directional_conviction or 0.0)
    )
    return LatestLLMFeatures(
        log_id=int(row.id),
        calibrated_conviction=calibrated,
        thesis_durability=float(row.thesis_durability or 0.0),
        catalyst_specificity=float(row.catalyst_specificity or 0.0),
        risk_flag=float(row.risk_flag or 0.0),
        is_present=True,
    )


async def write_bandit_propensity(
    log_id: int,
    *,
    posterior_mean: float,
    posterior_var: float,
    propensity: float,
) -> None:
    """Update llm_decision_log with the bandit propensity at decision time.

    Called from the quant orchestrator when an arm tied to an LLM-scored
    underlying is selected. IPS-reweighted calibration depends on this.
    Clamps the propensity to ``[1e-6, 1]`` so a downstream
    ``1 / propensity`` cannot blow up.
    """
    p = max(1e-6, min(1.0, float(propensity)))
    sql = text("""
        UPDATE llm_decision_log
        SET bandit_posterior_mean = :pm,
            bandit_posterior_var  = :pv,
            bandit_arm_propensity = :p
        WHERE id = :id
    """)
    try:
        async with session_scope() as session:
            await session.execute(sql, {
                "id": log_id, "pm": posterior_mean, "pv": posterior_var, "p": p,
            })
    except Exception as exc:
        logger.warning(f"llm_feature_lookup: propensity write failed for log_id={log_id}: {exc!r}")
