#!/usr/bin/env python3
"""CLI operator tool: replay a prior workflow_run (faithful or experimental).

Usage:
    python scripts/replay_workflow_run.py <workflow_run_id>
    python scripts/replay_workflow_run.py <workflow_run_id> --from-agent ceo_judge
    python scripts/replay_workflow_run.py <workflow_run_id> --override fno_expert=v2
    python scripts/replay_workflow_run.py <workflow_run_id> --override fno_expert=v2 --tag ab_fno_v2
    python scripts/replay_workflow_run.py <workflow_run_id> --dry-run

Environment variables required (same as main app):
    DATABASE_URL, ANTHROPIC_API_KEY, etc. (see .env.example)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# Make project root importable when running as a script
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("replay_workflow_run")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Replay a Laabh workflow_run.")
    p.add_argument("workflow_run_id", help="UUID of the original workflow_run.")
    p.add_argument(
        "--from-agent",
        default=None,
        metavar="AGENT_NAME",
        help="Only replay from this agent onward; earlier agents served from cache.",
    )
    p.add_argument(
        "--override",
        default=[],
        action="append",
        metavar="AGENT=VERSION",
        help="Persona version override, e.g. fno_expert=v2. Repeatable.",
    )
    p.add_argument(
        "--tag",
        default=None,
        metavar="EXPERIMENT_TAG",
        help="Experiment tag stored on the new workflow_run for grouping.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be replayed without actually running.",
    )
    return p.parse_args()


def _parse_overrides(raw: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for item in raw:
        if "=" not in item:
            raise ValueError(f"Invalid override {item!r} — expected AGENT=VERSION")
        agent, version = item.split("=", 1)
        overrides[agent.strip()] = version.strip()
    return overrides


async def _run(args: argparse.Namespace) -> None:
    from src.config import settings
    from src.db import get_async_session
    from src.agents.runtime import WorkflowRunner, replay_workflow_run

    overrides = _parse_overrides(args.override)

    if args.dry_run:
        log.info("DRY-RUN mode — no API calls or DB writes will be made.")
        log.info("Would replay workflow_run: %s", args.workflow_run_id)
        if args.from_agent:
            log.info("From agent: %s", args.from_agent)
        if overrides:
            log.info("Persona overrides: %s", json.dumps(overrides))
        if args.tag:
            log.info("Experiment tag: %s", args.tag)
        replay_type = "experimental" if overrides else "faithful"
        log.info("Replay type: %s (cost: %s)", replay_type,
                 "partial — overridden agents only" if overrides else "zero — served from audit log")
        return

    try:
        import anthropic
        anthropic_client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    except Exception as e:
        log.error("Failed to initialise Anthropic client: %s", e)
        sys.exit(1)

    telegram = None
    try:
        if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID:
            from src.services.notification_service import TelegramNotifier
            telegram = TelegramNotifier(settings.TELEGRAM_BOT_TOKEN)
    except Exception:
        pass

    runner = WorkflowRunner(
        db_session_factory=get_async_session,
        redis=None,
        anthropic=anthropic_client,
        telegram=telegram,
    )

    log.info(
        "Replaying workflow_run=%s from_agent=%s overrides=%s tag=%s",
        args.workflow_run_id,
        args.from_agent,
        overrides or "(faithful)",
        args.tag or "(none)",
    )

    result = await replay_workflow_run(
        runner=runner,
        original_workflow_run_id=args.workflow_run_id,
        from_agent=args.from_agent,
        persona_version_override=overrides if overrides else None,
        experiment_tag=args.tag,
    )

    log.info("Replay complete. New workflow_run_id: %s", result.workflow_run_id)
    log.info("Status: %s", result.status)
    log.info("Cost: $%.4f USD", float(result.total_cost_usd))

    if result.predictions:
        log.info("Predictions (%d):", len(result.predictions))
        for p in result.predictions:
            log.info("  %s", json.dumps(p, default=str))
    else:
        log.info("No predictions produced.")


def main() -> None:
    args = _parse_args()
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        log.info("Interrupted.")
        sys.exit(0)
    except Exception as e:
        log.error("Replay failed: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
