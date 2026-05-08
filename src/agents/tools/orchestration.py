"""Orchestration tool executor: get_full_rationale."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import text

from src.agents.tools._helpers import is_missing_table

if TYPE_CHECKING:
    from src.agents.tools.registry import ToolContext

log = logging.getLogger(__name__)


async def execute_get_full_rationale(params: dict, ctx: "ToolContext") -> dict:
    """Retrieve the full rationale for a prediction or candidate by ID.

    Tries agent_predictions first (by id), then falls back to signals.
    Tolerates missing agent_predictions table (pre-migration-0009).
    """
    item_id = params["prediction_or_candidate_id"]

    try:
        async with ctx.db() as db:
            # Try agent_predictions; degrade gracefully when table is missing.
            row = None
            try:
                result = await db.execute(
                    text("""
                        SELECT ap.id, ap.symbol_or_underlying, ap.decision,
                               ap.rationale, ap.judge_output, ap.created_at,
                               ap.conviction, ap.expected_pnl_pct
                        FROM agent_predictions ap
                        WHERE ap.id = :id
                        LIMIT 1
                    """),
                    {"id": str(item_id)},
                )
                row = result.fetchone()
            except Exception as inner:
                if not is_missing_table(inner):
                    raise
                # Postgres aborts the implicit txn after a relation error;
                # rollback so the next SELECT in this session can run.
                try:
                    await db.rollback()
                except Exception:
                    pass
                log.debug("agent_predictions table missing — skipping prediction lookup")
            if row:
                return {
                    "type": "agent_prediction",
                    "id": str(row[0]),
                    "symbol": row[1],
                    "decision": row[2],
                    "rationale": row[3],
                    "judge_output": row[4],
                    "created_at": str(row[5]),
                    "conviction": float(row[6] or 0),
                    "expected_pnl_pct": float(row[7] or 0) if row[7] else None,
                }

            # Try signals table
            result = await db.execute(
                text("""
                    SELECT s.id, i.symbol, s.action, s.reasoning,
                           s.confidence, s.signal_date,
                           s.entry_price, s.target_price, s.stop_loss,
                           a.name AS analyst_name
                    FROM signals s
                    JOIN instruments i ON i.id = s.instrument_id
                    LEFT JOIN analysts a ON a.id = s.analyst_id
                    WHERE s.id = :id
                    LIMIT 1
                """),
                {"id": str(item_id)},
            )
            row = result.fetchone()
            if row:
                return {
                    "type": "signal",
                    "id": str(row[0]),
                    "symbol": row[1],
                    "action": row[2],
                    "reasoning": row[3],
                    "confidence": float(row[4] or 0),
                    "signal_date": str(row[5]),
                    "entry_price": float(row[6]) if row[6] else None,
                    "target_price": float(row[7]) if row[7] else None,
                    "stop_loss": float(row[8]) if row[8] else None,
                    "analyst": row[9],
                }

            return {"result": None, "note": f"No prediction or signal found for id {item_id!r}"}

    except Exception as e:
        return {"result": None, "error": f"{type(e).__name__}: {e}"}
