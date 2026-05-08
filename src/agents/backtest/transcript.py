"""JSONL transcript writer — one line per LLM call.

Each line records the full system prompt, user prompt, model, output payload,
token counts, and the agent it came from. Designed for prompt-engineering
review, off-line evals, and feeding into prompt-version A/B sweeps.

Schema (one JSON object per line):
    {
        "ix": 1,
        "agent_name": "brain_triage",
        "persona_version": "v1",
        "model": "claude-haiku-4-5-20251001",
        "tool_name": "emit_brain_triage",
        "system_prompt": "<full text>",
        "user_prompt": "<full text>",
        "output_payload": {...},
        "input_tokens": 9539,
        "output_tokens": 850,
        "duration_ms": 9596,
        "status": "succeeded",
        "error": null
    }
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agents.backtest.runner import BacktestResult


def write_transcript(result: "BacktestResult", path: Path) -> None:
    """Write one JSONL line per agent invocation, with full prompt + response."""
    from src.agents.personas import PERSONA_MANIFEST

    calls_by_tool: dict[str, list[dict]] = defaultdict(list)
    for call in result.api_call_log or []:
        if call.get("tool_name"):
            calls_by_tool[call["tool_name"]].append(call)
    cursor: dict[str, int] = defaultdict(int)

    def _output_tool_for(agent_name: str, persona_version: str) -> str | None:
        defn = PERSONA_MANIFEST.get(agent_name, {}).get(persona_version, {})
        return defn.get("output_tool")

    with path.open("w", encoding="utf-8") as f:
        for i, ar in enumerate(result.agent_runs, 1):
            tool = _output_tool_for(ar["agent_name"], ar["persona_version"])
            call: dict = {}
            if tool and cursor[tool] < len(calls_by_tool[tool]):
                call = calls_by_tool[tool][cursor[tool]]
                cursor[tool] += 1

            row = {
                "ix": i,
                "workflow_run_id": result.workflow_run_id,
                "workflow_name": result.workflow_name,
                "target_date": result.target_date.isoformat(),
                "agent_name": ar["agent_name"],
                "persona_version": ar["persona_version"],
                "model": ar["model_used"],
                "tool_name": call.get("tool_name") or tool,
                "system_prompt": call.get("system_prompt") or "",
                "user_prompt": call.get("user_prompt") or "",
                "output_payload": ar.get("output") or call.get("response_payload"),
                "input_tokens": ar["input_tokens"],
                "output_tokens": ar["output_tokens"],
                "cost_usd": ar["cost_usd"],
                "duration_ms": ar["duration_ms"],
                "status": ar["status"],
                "error": ar.get("error"),
            }
            f.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")
