"""Mock Anthropic client for backtest dry-runs.

Returns schema-valid stub responses for every persona's `output_tool`. Zero
API cost, deterministic output. Used by `BacktestRunner` when `mock_llm=True`.

The stubs are hand-crafted to:
  * satisfy each persona's output JSON schema (so `_extract_tool_call` works);
  * satisfy the `CEOJudgeOutputValidated` cross-agent validator (so the workflow
    finalises as `succeeded`, not `succeeded_with_caveats`);
  * incorporate the candidate symbol from the prompt (so backtest reports show
    realistic, varying picks rather than identical placeholder names).

This is *not* a market-realistic LLM. Its job is to exercise the workflow
plumbing end-to-end so we can audit cost, structure, and validator behavior.
"""
from __future__ import annotations

import json
import re
import types
from dataclasses import dataclass
from typing import Any
from uuid import uuid4


# ---------------------------------------------------------------------------
# Anthropic-compatible response shapes
# ---------------------------------------------------------------------------

@dataclass
class _ToolUseBlock:
    """Mimics anthropic.types.ToolUseBlock duck-typed for WorkflowRunner."""
    name: str
    input: dict
    id: str
    type: str = "tool_use"

    def dict(self) -> dict:
        return {"type": self.type, "name": self.name, "input": self.input, "id": self.id}


@dataclass
class _TextBlock:
    text: str
    type: str = "text"

    def dict(self) -> dict:
        return {"type": self.type, "text": self.text}


@dataclass
class _Usage:
    input_tokens: int = 1500
    output_tokens: int = 400
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class _Message:
    content: list
    usage: _Usage
    stop_reason: str = "tool_use"
    id: str = ""


# ---------------------------------------------------------------------------
# Stub builders — one per output_tool name
# ---------------------------------------------------------------------------

def _extract_symbol_from_messages(messages: list[dict]) -> str | None:
    """Find the first `symbol` value in the user-turn messages."""
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    content = block.get("text", "")
                    break
        if isinstance(content, str):
            m = re.search(r'"symbol"\s*:\s*"([^"]+)"', content)
            if m:
                return m.group(1)
    return None


def _extract_universe_symbols(messages: list[dict], limit: int = 10) -> list[str]:
    """Extract up to `limit` symbols mentioned in the prompt."""
    seen: list[str] = []
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                b.get("text", "") for b in content if isinstance(b, dict)
            )
        if not isinstance(content, str):
            continue
        for sym in re.findall(r'"symbol"\s*:\s*"([^"]+)"', content):
            if sym not in seen:
                seen.append(sym)
                if len(seen) >= limit:
                    return seen
    return seen


def _build_brain_triage(messages: list[dict]) -> dict:
    syms = _extract_universe_symbols(messages, limit=8) or ["NIFTY", "RELIANCE", "TATAMOTORS"]
    fno = [s for s in syms if s in {"NIFTY", "BANKNIFTY", "FINNIFTY"}] or [syms[0]]
    eq = [s for s in syms if s not in fno][:2] or ["RELIANCE"]
    return {
        "as_of": "2026-05-07T09:00:00+05:30",
        "skip_today": False,
        "skip_reason": None,
        "fno_candidates": [
            {
                "symbol": s,
                "rank_score": 0.72,
                "primary_driver": f"[mock] {s} on the watchlist with neutral signal velocity",
                "watch_for": "intraday breakout above pre-market high",
                "expected_strategy_family": "directional_long",
            }
            for s in fno[:2]
        ],
        "equity_candidates": [
            {
                "symbol": s,
                "rank_score": 0.68,
                "primary_driver": f"[mock] {s} held by majority of brokers, recent positive flow",
                "watch_for": "follow-through volume after open",
                "horizon_hint": "3d",
            }
            for s in eq[:2]
        ],
        "explicit_skips": [],
        "regime_note": "[mock] VIX in middle band, neutral regime — no override.",
        "estimated_downstream_calls": {"fno_expert": len(fno[:2]), "equity_expert": len(eq[:2])},
    }


def _build_news_finder(messages: list[dict]) -> dict:
    sym = _extract_symbol_from_messages(messages) or "UNKNOWN"
    return {
        "instrument": {"symbol": sym},
        "as_of": "2026-05-07T09:00:00+05:30",
        "narrative": (
            f"[mock] No fresh material news found for {sym} in the last 18 hours. "
            f"Sentiment scan over recent raw_content is neutral with low signal "
            f"density. This stub is produced by the backtest mock client and "
            f"does not reflect real news scanning."
        ),
        "themes": ["[mock] no_material_news"],
        "catalysts_next_5d": [],
        "risk_flags": ["[mock] mock_run"],
        "citations": [],
        "summary_json": {
            "sentiment": "neutral",
            "score": 0.0,
            "signal_count": {"buy": 0, "sell": 0, "hold": 1},
            "top_analyst_views": [],
            "freshness_minutes": 60,
            "go_no_go_hint": "marginal",
        },
    }


def _build_news_editor(messages: list[dict]) -> dict:
    sym = _extract_symbol_from_messages(messages) or "UNKNOWN"
    return {
        "instrument_symbol": sym,
        "headline": f"[mock] No actionable news for {sym}",
        "credibility_grade": "C",
        "spike_or_noise": "noise",
        "go_no_go_for_brain": False,
        "weak_claims": [],
        "editor_note": "[mock] Backtest stub — no real editorial judgement applied.",
    }


def _build_explorer_trend(messages: list[dict]) -> dict:
    sym = _extract_symbol_from_messages(messages) or "UNKNOWN"
    return {
        "symbol": sym,
        "horizon_views": {
            "daily_trend": "sideways",
            "hourly_trend": "sideways",
            "key_support": 100.0,
            "key_resistance": 110.0,
            "rsi_14": 50.0,
        },
        "tradable_pattern": None,
        "volume_confirmation": False,
        "regime_break": False,
        "vs_benchmark": "in_line",
        "tldr": f"[mock] {sym}: no clear trend, ranging.",
    }


def _build_explorer_past_predictions(messages: list[dict]) -> dict:
    sym = _extract_symbol_from_messages(messages) or "UNKNOWN"
    return {
        "symbol": sym,
        "stats": {"n_predictions": 0, "win_rate": 0.0, "mean_pnl_pct": 0.0, "lookback_days": 30},
        "conviction_calibration": "insufficient_data",
        "biggest_win": None,
        "biggest_loss": None,
        "tradable_patterns": [],
        "do_not_repeat": [],
        "tldr": f"[mock] {sym}: no historical predictions to learn from.",
    }


def _build_explorer_sentiment_drift(messages: list[dict]) -> dict:
    sym = _extract_symbol_from_messages(messages) or "UNKNOWN"
    return {
        "symbol": sym,
        "sentiment_phase": "neutral",
        "today_vs_30d": {"today_score": 0.0, "avg_30d": 0.0, "delta": 0.0},
        "convergence_trend": "stable",
        "price_sentiment_divergence": False,
        "regime_shift": False,
        "tldr": f"[mock] {sym}: sentiment flat vs 30d baseline.",
    }


def _build_explorer_fno_positioning(messages: list[dict]) -> dict:
    sym = _extract_symbol_from_messages(messages) or "UNKNOWN"
    return {
        "symbol": sym,
        "oi_structure": {
            "pcr": 1.0,
            "max_pain": 100.0,
            "heavy_ce_strike": 110.0,
            "heavy_pe_strike": 90.0,
        },
        "expected_move_pct": 1.0,
        "iv_context": "fair",
        "positioning_signal": "neutral",
        "liquidity": "high",
        "tldr": f"[mock] {sym}: neutral OI structure, IV fair.",
    }


def _build_explorer_aggregator(messages: list[dict]) -> dict:
    sym = _extract_symbol_from_messages(messages) or "UNKNOWN"
    return {
        "symbol": sym,
        "tradable_pattern_score": 0.4,
        "dominant_horizon": "1w",
        "alignment_summary": (
            f"[mock] {sym}: trend, sentiment, and positioning sub-agents all "
            f"returned neutral. No tradable convergence."
        ),
        "signals_to_watch": ["volume spike on open", "VIX move >5%"],
        "do_not_repeat": [],
        "regime_consistency_with_today": "med",
        "tldr": f"[mock] {sym}: no edge today.",
    }


def _build_fno_expert(messages: list[dict]) -> dict:
    sym = _extract_symbol_from_messages(messages) or "NIFTY"
    return {
        "symbol": sym,
        "strategy": "bull_call_spread",
        "direction": "bullish",
        "conviction": 0.55,
        "expected_10pct_probability": 0.35,
        "legs": [
            {"action": "BUY", "option_type": "CE", "strike": 100.0,
             "expiry": "2026-05-15", "lots": 1},
            {"action": "SELL", "option_type": "CE", "strike": 110.0,
             "expiry": "2026-05-15", "lots": 1},
        ],
        "economics": {
            "max_loss_pct": 1.0,
            "target_pnl_pct": 4.0,
            "breakeven": 102.5,
            "transaction_cost_inr": 60.0,
        },
        "kill_switch": {
            "trigger_price": 95.0,
            "trigger_type": "spot_below",
            "action": "exit_all_at_market",
        },
        "thesis": (
            f"[mock] {sym}: defined-risk bull call spread on a neutral setup, "
            f"sized for 1% capital at risk. Backtest stub."
        ),
        "refused": False,
        "refuse_reason": None,
    }


def _build_equity_expert(messages: list[dict]) -> dict:
    sym = _extract_symbol_from_messages(messages) or "RELIANCE"
    return {
        "symbol": sym,
        "decision": "HOLD",
        "thesis": (
            f"[mock] {sym}: neutral signals across news, technicals, and "
            f"sentiment. No clear edge. Backtest stub."
        ),
        "entry_zone": {"low": 99.0, "high": 101.0},
        "target": 105.0,
        "stop": 97.0,
        "horizon": "5d",
        "conviction": 0.5,
        "expected_pnl_pct": 1.5,
        "max_loss_pct": 2.0,
        "capital_pct": 5.0,
        "catalyst_to_monitor": "next results / volume confirmation",
        "refused": False,
        "refuse_reason": None,
    }


def _build_ceo_bull(messages: list[dict]) -> dict:
    return {
        "stance": "bullish_measured",
        "core_thesis": (
            "[mock] Measured bullish stance — VIX neutral, no major catalyst risk, "
            "small directional positions in screened candidates."
        ),
        "top_3_evidence": [
            {
                "claim": "[mock] Brain triage flagged 1-2 candidates with rank > 0.7",
                "evidence_type": "signal",
                "provenance": {"signal_id": None, "raw_content_id": None,
                               "metric": "rank_score>0.7"},
                "weight": 0.6,
            },
            {
                "claim": "[mock] No high-impact macro events in next 5 days",
                "evidence_type": "macro",
                "provenance": {"signal_id": None, "raw_content_id": None,
                               "metric": "calendar_clean"},
                "weight": 0.5,
            },
            {
                "claim": "[mock] IV rank fair across F&O candidates",
                "evidence_type": "positioning",
                "provenance": {"signal_id": None, "raw_content_id": None,
                               "metric": "iv_context=fair"},
                "weight": 0.4,
            },
        ],
        "top_3_counter_to_other_side": [
            {
                "likely_other_side_claim": "[mock] No fresh news = no real edge",
                "rebuttal": "[mock] Defined-risk spreads survive on positioning, not news",
                "rebuttal_strength": "medium",
            }
        ],
        "preferred_allocation": [
            {"asset_class": "fno", "underlying_or_symbol": "NIFTY", "capital_pct": 1.5},
            {"asset_class": "cash", "underlying_or_symbol": "", "capital_pct": 98.5},
        ],
        "conviction": 0.55,
        "what_would_change_my_mind": [
            "[mock] VIX spike above 22 within first 30 minutes",
            "[mock] NIFTY gap-down >1% on open",
            "[mock] Surprise RBI commentary triggering rate uncertainty",
        ],
    }


def _build_ceo_bear(messages: list[dict]) -> dict:
    return {
        "stance": "bearish_measured",
        "core_thesis": (
            "[mock] Measured bearish — capital preservation dominates absent a "
            "specific edge. Cash is a position."
        ),
        "top_3_evidence": [
            {
                "claim": "[mock] No fresh material news on candidates",
                "evidence_type": "signal",
                "provenance": {},
                "weight": 0.6,
            },
            {
                "claim": "[mock] Trend sub-agents returned 'sideways' across candidates",
                "evidence_type": "technical",
                "provenance": {},
                "weight": 0.5,
            },
            {
                "claim": "[mock] Past predictions data is sparse — overconfidence risk",
                "evidence_type": "positioning",
                "provenance": {},
                "weight": 0.4,
            },
        ],
        "top_3_counter_to_other_side": [
            {
                "likely_other_side_claim": "[mock] Defined-risk = small downside",
                "rebuttal": "[mock] Small downside still nets negative expected value over "
                           "many no-edge days",
                "rebuttal_strength": "medium",
            }
        ],
        "preferred_allocation": [
            {"asset_class": "cash", "underlying_or_symbol": "", "capital_pct": 100.0}
        ],
        "conviction": 0.6,
        "what_would_change_my_mind": [
            "[mock] Sudden surge in convergent buy signals across watchlist (>5 in 60 min)",
            "[mock] India VIX cracking below 12 with NIFTY breakout above pre-market high",
            "[mock] Tier-1 broker upgrade with target >10% above spot",
        ],
    }


def _build_ceo_judge(messages: list[dict]) -> dict:
    """Final allocation: small fno position (1.5%) + cash. Passes all validators."""
    return {
        "decision_summary": (
            "[mock] Bull and Bear converge on caution — measured 1.5% fno "
            "deployment with kill-switches; remaining 98.5% in cash. Backtest stub."
        ),
        "disagreement_loci": [
            {
                "topic": "fno deployment vs all-cash",
                "bull_view": "small spread is positive EV given fair IV",
                "bear_view": "no fresh news = no edge, all-cash is correct",
                "judge_lean": "bull",
                "lean_strength": "weak",
                "decisive_evidence": "fair IV + defined risk dominates the marginal cost",
            }
        ],
        "allocation": [
            {
                "asset_class": "fno",
                "underlying_or_symbol": "NIFTY",
                "capital_pct": 1.5,
                "decision": "bull_call_spread",
                "horizon": "1w",
                "conviction": 0.55,
            },
            {
                "asset_class": "cash",
                "underlying_or_symbol": "",
                "capital_pct": 98.5,
                "decision": "hold_cash",
                "horizon": None,
                "conviction": 0.6,
            },
        ],
        "expected_book_pnl_pct": 0.5,
        "stretch_pnl_pct": 1.0,
        "max_drawdown_tolerated_pct": 2.0,
        "kill_switches": [
            {
                "trigger": "India VIX spikes above 22 in the first 30 minutes",
                "action": "exit_all",
                "monitoring_metric": "VIX > 22",
            },
            {
                "trigger": "NIFTY gap-down opens >1% below previous close",
                "action": "scale_down_50",
                "monitoring_metric": "NIFTY open < prev_close × 0.99",
            },
        ],
        "ceo_note": (
            "[mock] Today is a defensive day. Capital preservation dominates. "
            "Single small spread to keep skin in the game; rest in cash. "
            "Watch VIX and NIFTY open for the kill-switch triggers above. "
            "Re-evaluate at 10:30 if either trigger fires. "
            "This is a backtest mock and not a live recommendation."
        ),
        "calibration_self_check": {
            "bullish_argument_grade": "B",
            "bearish_argument_grade": "B",
            "confidence_in_allocation": 0.6,
            "regret_scenario": "Regret asymmetric on the bull side (missed rally > absorbed loss)",
        },
    }


def _build_shadow_evaluator(messages: list[dict]) -> dict:
    return {
        "workflow_run_id": "00000000-0000-0000-0000-000000000000",
        "scores": {
            "calibration": {"score": 7.0, "rationale": "[mock] reasonable defensive stance"},
            "evidence_alignment": {"score": 7.0, "rationale": "[mock] evidence sparse but consistent"},
            "guardrail_proximity": {"score": 9.0, "near_misses": []},
            "novelty": {"score": 6.0, "is_re_skin": False, "is_repeat_mistake": False,
                        "matched_history_run_ids": []},
            "self_consistency": {"score": 8.0, "inconsistencies": []},
        },
        "headline_concern": None,
        "alert_operator": False,
    }


# Tool name → builder function
_BUILDERS: dict[str, Any] = {
    "emit_brain_triage": _build_brain_triage,
    "emit_news_finder": _build_news_finder,
    "emit_news_editor": _build_news_editor,
    "emit_explorer_trend": _build_explorer_trend,
    "emit_explorer_past_predictions": _build_explorer_past_predictions,
    "emit_explorer_sentiment_drift": _build_explorer_sentiment_drift,
    "emit_explorer_fno_positioning": _build_explorer_fno_positioning,
    "emit_explorer_aggregator": _build_explorer_aggregator,
    "emit_fno_expert": _build_fno_expert,
    "emit_equity_expert": _build_equity_expert,
    "emit_ceo_bull": _build_ceo_bull,
    "emit_ceo_bear": _build_ceo_bear,
    "emit_ceo_judge": _build_ceo_judge,
    "emit_shadow_evaluator": _build_shadow_evaluator,
}


# ---------------------------------------------------------------------------
# Mock Anthropic client
# ---------------------------------------------------------------------------

class _MockMessages:
    """Implements the `anthropic.AsyncAnthropic.messages` API surface."""

    def __init__(self, owner: "MockAnthropicClient") -> None:
        self._owner = owner

    async def create(self, **kwargs) -> _Message:
        """Single non-streaming call. Returns a fake `Message` matching tool_choice."""
        self._owner.calls += 1
        resp = _build_response(kwargs)
        self._owner.record(kwargs, resp)
        return resp

    def stream(self, **kwargs):
        """Streaming context manager. Mimics `messages.stream(...)`."""
        return _MockStream(kwargs, self._owner)


class _MockStream:
    """Async context manager matching `anthropic.AsyncAnthropic.messages.stream(...)`."""

    def __init__(self, kwargs: dict, owner: "MockAnthropicClient") -> None:
        self._kwargs = kwargs
        self._owner = owner
        self._final: _Message | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def __aiter__(self):
        async def _gen():
            # Emit a single delta event so streaming loops have something to consume.
            yield types.SimpleNamespace(
                type="content_block_delta",
                delta=types.SimpleNamespace(text="[mock-stream]"),
            )
        return _gen()

    async def get_final_message(self) -> _Message:
        if self._final is None:
            self._owner.calls += 1
            self._final = _build_response(self._kwargs)
            self._owner.record(self._kwargs, self._final)
        return self._final


def _build_response(api_kwargs: dict) -> _Message:
    """Construct a fake Anthropic `Message` from request kwargs.

    The runtime's data-tool pre-loop overrides `tool_choice` to `auto` on
    intermediate turns, so the mock also inspects the request's `tools` list:
    if any tool there matches a known `emit_*` builder, we call it directly.
    This skips the data-tool dance entirely (which is the right behaviour for
    a mock — there is no DB to query).
    """
    tool_choice = api_kwargs.get("tool_choice", {})
    forced_tool: str | None = None
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "tool":
        forced_tool = tool_choice.get("name")

    if not forced_tool:
        for t in api_kwargs.get("tools", []) or []:
            name = t.get("name") if isinstance(t, dict) else None
            if name in _BUILDERS:
                forced_tool = name
                break

    messages = api_kwargs.get("messages", [])

    if forced_tool and forced_tool in _BUILDERS:
        builder = _BUILDERS[forced_tool]
        payload = builder(messages)
        block = _ToolUseBlock(name=forced_tool, input=payload, id=f"toolu_{uuid4().hex[:8]}")
        return _Message(content=[block], usage=_Usage(), stop_reason="tool_use")

    # No matching tool: emit a text block (fallback path).
    return _Message(
        content=[_TextBlock(text="[mock] no tool forced; emitting placeholder text.")],
        usage=_Usage(),
        stop_reason="end_turn",
    )


class MockAnthropicClient:
    """Drop-in replacement for `anthropic.AsyncAnthropic` for backtests.

    Only implements the surface area the WorkflowRunner uses:
      * `client.messages.create(**kwargs)`  — non-streaming
      * `client.messages.stream(**kwargs)`  — streaming context manager

    Records every call to `self.history` so the backtest report can render
    the prompt and response side-by-side per agent invocation.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.history: list[dict] = []
        self.messages = _MockMessages(self)

    def record(self, kwargs: dict, response: _Message) -> None:
        """Append one prompt/response pair to history."""
        # Extract the forced tool name (or any builder-matched tool)
        tc = kwargs.get("tool_choice", {})
        forced = tc.get("name") if isinstance(tc, dict) and tc.get("type") == "tool" else None
        if not forced:
            for t in kwargs.get("tools", []) or []:
                name = t.get("name") if isinstance(t, dict) else None
                if name in _BUILDERS:
                    forced = name
                    break

        # Render system prompt to a string (Anthropic accepts list-of-blocks too).
        sys_block = kwargs.get("system", "")
        if isinstance(sys_block, list):
            sys_text = "".join(b.get("text", "") for b in sys_block if isinstance(b, dict))
        else:
            sys_text = str(sys_block)

        # Extract user-turn content as a single string for display.
        user_text = ""
        for m in kwargs.get("messages", []) or []:
            if m.get("role") != "user":
                continue
            c = m.get("content", "")
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "text":
                        user_text += b.get("text", "")
            elif isinstance(c, str):
                user_text += c

        # Tool-use block payload from the response, if any.
        tool_payload = None
        for block in response.content or []:
            if getattr(block, "type", None) == "tool_use":
                tool_payload = getattr(block, "input", None)
                break
        if tool_payload is None:
            for block in response.content or []:
                if getattr(block, "type", None) == "text":
                    tool_payload = {"_text": getattr(block, "text", "")}
                    break

        self.history.append({
            "model": kwargs.get("model"),
            "tool_name": forced,
            "system_prompt": sys_text,
            "user_prompt": user_text,
            "response_payload": tool_payload,
            "stop_reason": response.stop_reason,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        })
