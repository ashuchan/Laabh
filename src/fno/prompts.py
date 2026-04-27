"""Versioned prompt templates for the F&O thesis synthesizer.

All prompts must return a valid JSON object with the schema defined below.
Increment FNO_THESIS_PROMPT_VERSION whenever the prompt or schema changes.
"""
from __future__ import annotations

FNO_THESIS_PROMPT_VERSION = "v1"

# Expected JSON response schema (for documentation + parsing validation):
# {
#   "decision": "PROCEED" | "SKIP" | "HEDGE",
#   "direction": "bullish" | "bearish" | "neutral",
#   "thesis": "<one paragraph max, 120 words>",
#   "risk_factors": ["<factor 1>", "<factor 2>"],
#   "confidence": 0.0-1.0
# }

FNO_THESIS_SYSTEM = """\
You are a professional F&O (Futures & Options) analyst for Indian equity markets (NSE/BSE).
Your task is to synthesize multi-factor signals into a concise trading thesis for an
options strategy selection engine.

Always respond with a single valid JSON object — no markdown, no preamble.
Schema:
{
  "decision": "PROCEED" | "SKIP" | "HEDGE",
  "direction": "bullish" | "bearish" | "neutral",
  "thesis": "<120-word max paragraph explaining the trade rationale>",
  "risk_factors": ["<up to 3 key risks>"],
  "confidence": <0.0 to 1.0>
}

Rules:
- PROCEED: sufficient directional conviction (confidence ≥ 0.55) for a directional option strategy
- HEDGE: signals are mixed or uncertain — calendar/iron-condor/straddle may be appropriate
- SKIP: no edge detected; avoid trading this instrument today
- Base confidence on agreement between news, macro, FII/DII, and technical signals
- Risk factors must be specific to this instrument and current market context
- Keep thesis concise and actionable — no speculation beyond provided data
"""

FNO_THESIS_USER_TEMPLATE = """\
Instrument: {symbol} ({sector})
Underlying price: ₹{underlying_price}
IV Rank (52w): {iv_rank}%
IV Regime: {iv_regime}
OI Structure: {oi_structure}
Days to nearest expiry: {days_to_expiry}

Catalyst scores (0=max bearish, 10=max bullish):
- News signals: {news_score}/10 ({bullish_count} bullish, {bearish_count} bearish in last {lookback_hours}h)
- Market sentiment: {sentiment_score}/10
- FII/DII activity: {fii_dii_score}/10 (FII net ₹{fii_net_cr}Cr, DII net ₹{dii_net_cr}Cr)
- Macro alignment: {macro_align_score}/10 (key drivers: {macro_drivers})
- Convergence: {convergence_score}/10
- Composite: {composite_score}/10

Recent news headlines:
{headlines}

Generate the thesis JSON.
"""
