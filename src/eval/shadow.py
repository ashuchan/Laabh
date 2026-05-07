"""Shadow eval persistence — writes agent_predictions_eval rows."""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import text

if TYPE_CHECKING:
    from src.agents.runtime.spec import AgentRunResult, WorkflowContext

log = logging.getLogger(__name__)

_MAX_EVAL_INPUT_CHARS = 48_000   # ~12k tokens at 4 chars/token; hard cap for shadow eval


def _truncate_for_eval(stage_outputs: dict, max_chars: int = _MAX_EVAL_INPUT_CHARS) -> dict:
    """Shrink stage_outputs so it fits within the shadow evaluator's token budget.

    Strategy (order matters — most lossy stages trimmed first):
    1. Each explorer sub-output list is truncated to its first element.
    2. Long string values (>2000 chars) inside any dict value are trimmed.
    3. If the serialised size still exceeds max_chars, trim the whole blob to max_chars.
    """
    import copy

    truncated = copy.deepcopy(stage_outputs)

    # Step 1: trim explorer lists — keep only the first result per symbol
    for key in list(truncated.keys()):
        if key.startswith("explorer_") and isinstance(truncated[key], list):
            truncated[key] = truncated[key][:1]

    # Step 2: trim long string leaf values
    def _trim_strings(obj: object, limit: int = 2_000) -> object:
        if isinstance(obj, str):
            return obj[:limit] + "…[trimmed]" if len(obj) > limit else obj
        if isinstance(obj, dict):
            return {k: _trim_strings(v, limit) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_trim_strings(v, limit) for v in obj]
        return obj

    truncated = _trim_strings(truncated)  # type: ignore[arg-type]

    # Step 3: final size guard — hard truncate the serialised blob
    serialised = json.dumps(truncated)
    if len(serialised) > max_chars:
        serialised = serialised[:max_chars]
        log.warning(
            "shadow eval input truncated to %d chars (stage_outputs too large)", max_chars
        )
        # Return the raw truncated string as a special key so the evaluator
        # knows the inputs were clipped rather than receiving broken JSON.
        return {"_truncated_blob": serialised, "_truncation_note": "inputs exceeded eval budget"}

    return truncated


async def persist_shadow_eval_output(
    result: "AgentRunResult", ctx: "WorkflowContext"
) -> None:
    """Write the shadow_evaluator's output to agent_predictions_eval.

    Called by WorkflowRunner via POST_PROCESSORS when agent_name == 'shadow_evaluator'.
    """
    if result.status != "succeeded":
        return

    output = result.output or {}
    scores = output.get("scores") or {}

    async with ctx.db_session_factory() as db:
        await db.execute(
            text("""
                INSERT INTO agent_predictions_eval (
                    id, workflow_run_id, prediction_id, evaluator_agent_run_id,
                    evaluator_persona_version,
                    calibration_score, evidence_alignment_score,
                    guardrail_proximity_score, novelty_score, self_consistency_score,
                    headline_concern, is_re_skin, is_repeat_mistake,
                    matched_history_run_ids, near_misses, inconsistencies,
                    eval_cost_usd, created_at
                ) VALUES (
                    :id, :wfr, :pid, :ear, :pv,
                    :cal, :ea, :gp, :nov, :sc,
                    :hc, :rs, :rm, :mh, :nm, :inc,
                    :cost, NOW()
                )
            """),
            {
                "id": str(uuid4()),
                "wfr": ctx.workflow_run_id,
                "pid": ctx.stage_outputs.get("prediction_id"),
                "ear": result.agent_run_id,
                "pv": result.persona_version,
                "cal": _score(scores, "calibration"),
                "ea":  _score(scores, "evidence_alignment"),
                "gp":  _score(scores, "guardrail_proximity"),
                "nov": _score(scores, "novelty"),
                "sc":  _score(scores, "self_consistency"),
                "hc": output.get("headline_concern"),
                "rs": _flag(scores, "novelty", "is_re_skin"),
                "rm": _flag(scores, "novelty", "is_repeat_mistake"),
                "mh": json.dumps(_list(scores, "novelty", "matched_history_run_ids")),
                "nm": json.dumps(_list(scores, "guardrail_proximity", "near_misses")),
                "inc": json.dumps(_list(scores, "self_consistency", "inconsistencies")),
                "cost": float(result.cost_usd),
            },
        )
        await db.commit()

    if output.get("alert_operator"):
        min_score = min(
            (s.get("score", 10) for s in scores.values() if isinstance(s, dict)),
            default=10,
        )
        log.warning(
            f"Shadow eval flagged workflow {ctx.workflow_run_id}: "
            f"{output.get('headline_concern')} (min score {min_score}/10)"
        )
        if ctx.telegram:
            try:
                from src.config import settings
                await ctx.telegram.send(
                    chat_id=settings.TELEGRAM_CHAT_ID,
                    text=(
                        f"⚠️ Shadow eval flagged today's run\n"
                        f"Workflow: {ctx.workflow_run_id}\n"
                        f"Concern: {output.get('headline_concern')}\n"
                        f"Lowest score: {min_score}/10"
                    ),
                )
            except Exception as e:
                log.warning(f"Telegram alert failed: {e}")


def _score(scores: dict, dim: str) -> float | None:
    return (scores.get(dim) or {}).get("score")


def _flag(scores: dict, dim: str, key: str) -> bool:
    return bool((scores.get(dim) or {}).get(key, False))


def _list(scores: dict, dim: str, key: str) -> list:
    return (scores.get(dim) or {}).get(key, [])


async def fetch_recent_overall_scores(db_session_factory, days: int = 5) -> list[float]:
    """Fetch the last N days of overall_score values from agent_predictions_eval."""
    try:
        async with db_session_factory() as db:
            result = await db.execute(
                text("""
                    SELECT overall_score
                    FROM agent_predictions_eval
                    WHERE created_at >= NOW() - (:days || ' days')::INTERVAL
                    ORDER BY created_at DESC
                """),
                {"days": days},
            )
            rows = result.fetchall()
            return [float(r[0]) for r in rows if r[0] is not None]
    except Exception as e:
        log.warning(f"Failed to fetch recent eval scores: {e}")
        return []


async def check_daily_eval_alerts(
    db_session_factory,
    telegram=None,
    chat_id: str | None = None,
    rolling_days: int = 3,
    alert_threshold: float = 6.0,
) -> bool:
    """Fire a Telegram alert if the 3-day rolling average eval score drops below threshold.

    Called once per day after the shadow eval run.  Returns True if an alert was sent.
    """
    scores = await fetch_recent_overall_scores(db_session_factory, days=rolling_days)
    if not scores:
        log.info("check_daily_eval_alerts: no recent scores, skipping")
        return False

    avg = sum(scores) / len(scores)
    if avg >= alert_threshold:
        return False

    msg = (
        f"⚠️ Shadow eval degradation alert\n"
        f"3-day rolling avg score: {avg:.1f}/10 (threshold {alert_threshold})\n"
        f"Samples: {len(scores)} runs over the last {rolling_days} days\n"
        f"Action: review recent prompts and agent outputs."
    )
    log.warning(msg)

    if telegram and chat_id:
        try:
            await telegram.send(chat_id=chat_id, text=msg)
        except Exception as e:
            log.warning(f"Telegram alert for eval degradation failed: {e}")

    return True
