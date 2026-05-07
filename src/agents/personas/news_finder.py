"""News Finder persona — curates and synthesises news for one instrument."""
from __future__ import annotations

from src.agents.personas.shared import INDIAN_MARKET_DOMAIN_RULES, INTRADAY_BRIEF

NEWS_FINDER_PERSONA_V1 = f"""IDENTITY
You are the research analyst for an Indian equity paper-trading desk. Your job is
to synthesise all available news, filings, analyst notes, and podcast transcripts
for ONE instrument into a structured intelligence brief that informs the trading
decision. You are a journalist-analyst hybrid: sceptical of hype, strict about
sourcing, balanced in presenting bull and bear cases.

MANDATE
Return a structured news brief for the given instrument, covering only today and
the past 5 trading days. The brief must cite specific sources, score analyst
credibility, and arrive at a go/no-go hint for the desk.

INPUTS
- instrument: {{id, symbol, sector}}
- as_of: ISO timestamp
- The raw_content, filings, and transcript_chunks are accessible via your tools.

REASONING SCAFFOLD
1. Call search_raw_content for the instrument over the past 5 days.
2. Call get_filings for any SEBI/BSE/NSE filings in the same window.
3. Call search_transcript_chunks for recent analyst commentary.
4. For each piece of content: assign a credibility weight using the source table.
5. Identify convergence: do ≥2 credible sources agree on direction?
6. Draft the narrative in three paragraphs: (a) what happened, (b) analyst views,
   (c) risks and counterarguments.
7. Compute sentiment score as credibility-weighted average of bullish/bearish signals.
8. Emit refusal (go_no_go_hint=no_signal) if: only promoter statements, <2 credible
   sources, or news is >3 days stale with nothing new.

{INDIAN_MARKET_DOMAIN_RULES}

CALIBRATION
Credibility weights: Tier-1 broker 0.85, Tier-1 press 0.75, Domestic broker 0.65,
TV analyst 0.40-0.80 (use DB track record), Twitter 0.20, Promoter 0.30.
sentiment_score: +1.0 = unambiguously bullish (convergent, high credibility),
-1.0 = unambiguously bearish. Mostly noise = 0.0.
"""

NEWS_FINDER_OUTPUT_TOOL = {
    "name": "emit_news_finder",
    "description": "Emit the structured news brief for one instrument.",
    "input_schema": {
        "type": "object",
        "properties": {
            "instrument": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "symbol": {"type": "string"},
                },
                "required": ["symbol"],
            },
            "as_of": {"type": "string"},
            "narrative": {"type": "string", "minLength": 200, "maxLength": 4000},
            "themes": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
            "catalysts_next_5d": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "event": {"type": "string"},
                        "date": {"type": "string"},
                        "expected_impact": {"type": "string", "enum": ["high", "medium", "low"]},
                    },
                    "required": ["event", "expected_impact"],
                },
            },
            "risk_flags": {"type": "array", "maxItems": 5, "items": {"type": "string"}},
            "citations": {
                "type": "array",
                "maxItems": 30,
                "items": {
                    "type": "object",
                    "properties": {
                        "ref": {"type": "string", "pattern": "^c\\d+$"},
                        "raw_content_id": {"type": ["integer", "null"]},
                        "weight": {"type": "number"},
                        "analyst_credibility": {"type": "number"},
                    },
                    "required": ["ref", "weight"],
                },
            },
            "summary_json": {
                "type": "object",
                "properties": {
                    "sentiment": {"type": "string", "enum": ["bullish", "neutral", "bearish"]},
                    "score": {"type": "number", "minimum": -1, "maximum": 1},
                    "signal_count": {
                        "type": "object",
                        "properties": {
                            "buy": {"type": "integer"},
                            "sell": {"type": "integer"},
                            "hold": {"type": "integer"},
                        },
                    },
                    "top_analyst_views": {
                        "type": "array",
                        "maxItems": 5,
                        "items": {
                            "type": "object",
                            "properties": {
                                "analyst": {"type": "string"},
                                "stance": {"type": "string"},
                                "credibility": {"type": "number"},
                                "target": {"type": ["number", "null"]},
                            },
                            "required": ["analyst", "stance"],
                        },
                    },
                    "freshness_minutes": {"type": "integer"},
                    "go_no_go_hint": {"type": "string", "enum": ["go", "marginal", "no_signal"]},
                },
                "required": ["sentiment", "score", "go_no_go_hint"],
            },
        },
        "required": ["instrument", "as_of", "narrative", "summary_json"],
    },
}

NEWS_FINDER_PERSONA_V_INTRADAY = NEWS_FINDER_PERSONA_V1 + "\n\n" + INTRADAY_BRIEF

PERSONA_DEF = {
    "v1": {
        "model": "claude-sonnet-4-6",
        "fallback_model": "claude-haiku-4-5-20251001",
        "tools": ("search_raw_content", "get_filings", "search_transcript_chunks",
                  "get_analyst_track_record"),
        "output_tool": "emit_news_finder",
        "max_input_tokens": 16_000,
        "max_output_tokens": 2_500,
        "temperature": 0.1,
        "cost_class": "medium",
        "system_prompt": NEWS_FINDER_PERSONA_V1,
    },
    "v_intraday": {
        "model": "claude-haiku-4-5-20251001",
        "fallback_model": None,
        "tools": ("search_raw_content",),
        "output_tool": "emit_news_finder",
        "max_input_tokens": 8_000,
        "max_output_tokens": 1_000,
        "temperature": 0.0,
        "cost_class": "cheap",
        "system_prompt": NEWS_FINDER_PERSONA_V_INTRADAY,
    },
}
