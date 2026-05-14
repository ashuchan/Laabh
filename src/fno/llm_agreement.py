"""v9-vs-synthetic-v10 agreement-matrix diagnostic — Plan §1.4.

A small helper that derives a synthetic categorical decision from the v10
continuous output (using the plan's stated rule: PROCEED when
``|directional_conviction| > 0.4`` AND ``thesis_durability > 0.5``;
HEDGE when conviction is mid-range; SKIP otherwise) and tallies a 3×3
agreement matrix versus the v9 categorical decision over the same
(run_date, instrument_id).

Output is the 3×3 count table plus the headline rate the plan asks
about: "v10 PROCEED on v9-SKIP names" — when > 50%, the v9 gate is
over-rejecting (the original hypothesis driving the whole initiative).

Usage:
  >>> from src.fno.llm_agreement import compute_agreement
  >>> result = await compute_agreement(days=10)
  >>> result["v10_proceed_on_v9_skip_pct"]
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from loguru import logger
from sqlalchemy import text

from src.db import session_scope


# Plan §1.4 rule for deriving synthetic v10 labels.
_PROCEED_CONVICTION_MIN = 0.4
_PROCEED_DURABILITY_MIN = 0.5
_HEDGE_CONVICTION_MIN = 0.2

_Label = Literal["PROCEED", "HEDGE", "SKIP"]


def synthetic_v10_label(
    *,
    directional_conviction: float | None,
    thesis_durability: float | None,
    catalyst_specificity: float | None = None,
) -> _Label:
    """Map v10 continuous output to a v9-compatible PROCEED/HEDGE/SKIP label.

    Plan §1.4 specifies the PROCEED threshold; HEDGE / SKIP partitioning
    is mine: HEDGE for mid-conviction with weaker durability (uncertain
    but still actionable), SKIP for everything else.
    """
    if directional_conviction is None or thesis_durability is None:
        return "SKIP"
    abs_dc = abs(float(directional_conviction))
    durability = float(thesis_durability)
    if abs_dc > _PROCEED_CONVICTION_MIN and durability > _PROCEED_DURABILITY_MIN:
        return "PROCEED"
    if abs_dc > _HEDGE_CONVICTION_MIN:
        return "HEDGE"
    return "SKIP"


async def compute_agreement(
    *,
    days: int = 10,
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> dict:
    """Build the v9-vs-synthetic-v10 agreement matrix over the last ``days``
    trading days.

    Returns a dict containing:
      * ``matrix``: 3×3 count table keyed by (v9_label, v10_label).
      * ``v10_proceed_on_v9_skip_pct``: the headline rate (the gate-
        over-rejection check from plan §1.4).
      * ``n_paired``: number of (run_date, instrument_id) pairs scored.
    """
    upper = as_of or datetime.now(tz=timezone.utc)
    lower = upper - timedelta(days=int(days * 1.5))   # weekend buffer

    sql = text("""
        WITH v9 AS (
            SELECT run_date, instrument_id, decision_label
            FROM llm_decision_log
            WHERE prompt_version = 'v9'
              AND phase          = 'fno_thesis'
              AND as_of >= :lo AND as_of <= :hi
              AND ((:dryrun_run_id IS NULL  AND dryrun_run_id IS NULL)
                OR  dryrun_run_id = :dryrun_run_id)
        ),
        v10 AS (
            SELECT run_date, instrument_id,
                   directional_conviction, thesis_durability, catalyst_specificity
            FROM llm_decision_log
            WHERE prompt_version = 'v10_continuous'
              AND phase          = 'fno_thesis'
              AND directional_conviction IS NOT NULL
              AND as_of >= :lo AND as_of <= :hi
              AND ((:dryrun_run_id IS NULL  AND dryrun_run_id IS NULL)
                OR  dryrun_run_id = :dryrun_run_id)
        )
        SELECT v9.decision_label, v10.directional_conviction,
               v10.thesis_durability, v10.catalyst_specificity
        FROM v9 INNER JOIN v10
          ON v9.run_date = v10.run_date AND v9.instrument_id = v10.instrument_id
    """)
    async with session_scope() as session:
        rows = (await session.execute(sql, {
            "lo": lower,
            "hi": upper,
            "dryrun_run_id": str(dryrun_run_id) if dryrun_run_id else None,
        })).all()

    matrix: dict[tuple[str, str], int] = {
        (a, b): 0 for a in ("PROCEED", "HEDGE", "SKIP")
                  for b in ("PROCEED", "HEDGE", "SKIP")
    }
    n_paired = 0
    for r in rows:
        v9_lbl = (r.decision_label or "SKIP").upper()
        if v9_lbl not in ("PROCEED", "HEDGE", "SKIP"):
            v9_lbl = "SKIP"
        v10_lbl = synthetic_v10_label(
            directional_conviction=r.directional_conviction,
            thesis_durability=r.thesis_durability,
            catalyst_specificity=r.catalyst_specificity,
        )
        matrix[(v9_lbl, v10_lbl)] += 1
        n_paired += 1

    skip_total = sum(matrix[("SKIP", b)] for b in ("PROCEED", "HEDGE", "SKIP"))
    v10_proceed_on_v9_skip = matrix[("SKIP", "PROCEED")]
    pct = (v10_proceed_on_v9_skip / skip_total) if skip_total else 0.0

    logger.info(
        "llm_agreement: n_paired={} v10-PROCEED-on-v9-SKIP={}/{} ({:.1%})".format(
            n_paired, v10_proceed_on_v9_skip, skip_total, pct
        )
    )
    return {
        "n_paired": n_paired,
        "matrix": {f"{a}/{b}": c for (a, b), c in matrix.items()},
        "v10_proceed_on_v9_skip_pct": pct,
        "v10_proceed_on_v9_skip_count": v10_proceed_on_v9_skip,
        "v9_skip_total": skip_total,
        "gate_over_rejecting": pct > 0.50,
    }
