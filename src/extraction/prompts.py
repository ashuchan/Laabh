# ============================================================================
# LAABH — LLM Prompt Templates for Signal Extraction
# Used by src/extraction/llm_extractor.py
# ============================================================================

# Version these prompts — track which version produced each extraction
PROMPT_VERSION = "1.0.0"

# ============================================================================
# SYSTEM PROMPT (shared across all extraction types)
# ============================================================================

SYSTEM_PROMPT = """You are a financial signal extractor for Indian stock markets (BSE/NSE).

Your job is to analyze financial text — news articles, TV transcripts, analyst commentary — 
and extract actionable trading signals with structured data.

RULES:
1. Only extract signals with SPECIFIC stock mentions. "Markets look bullish" = no signal.
2. Accept text in English, Hindi, Hinglish (code-mixed). Handle all seamlessly.
3. Map common abbreviations: RIL/Reliance = RELIANCE, Infy = INFY, HDFC twins = HDFCBANK + HDFC,
   TaMo = TATAMOTORS, Bajaj Fin = BAJFINANCE, SBI = SBIN, L&T = LT, M&M = M&M
4. Confidence scoring:
   - 0.9+: explicit "buy X at Y, target Z" with specific prices
   - 0.7-0.8: clear directional call with reasoning but no specific targets
   - 0.5-0.6: implied sentiment from news (e.g., "strong quarterly results")
   - <0.5: vague or speculative mentions
5. For target prices: extract exact numbers if mentioned. If not mentioned, set null.
6. Distinguish between analyst opinions and factual reporting.
7. If text contains NO tradeable signals, return empty signals array — don't force-extract.
8. Timeframe mapping:
   - "intraday" / "aaj ka target" / "today" = intraday
   - "short term" / "1-2 weeks" / "near term" = short_term  
   - "medium term" / "1-3 months" = medium_term
   - "long term" / "1 year+" / "multibagger" = long_term

Return ONLY valid JSON. No markdown, no explanation, no preamble."""

# ============================================================================
# NEWS ARTICLE EXTRACTION
# ============================================================================

NEWS_EXTRACTION_PROMPT = """Analyze this financial news article from an Indian market source.
Extract any stock-specific trading signals.

Source: {source_name}
Title: {title}
Published: {published_at}

Article Text:
{content}

Return JSON:
{{
  "signals": [
    {{
      "stock_symbol": "NSE symbol (e.g., RELIANCE, TCS, HDFCBANK)",
      "company_name": "Full company name",
      "action": "BUY | SELL | HOLD | WATCH",
      "target_price": null or number,
      "stop_loss": null or number,
      "entry_price": null or number,
      "timeframe": "intraday | short_term | medium_term | long_term",
      "confidence": 0.0 to 1.0,
      "reasoning": "One sentence explaining why"
    }}
  ],
  "market_sentiment": "bullish | bearish | neutral",
  "sectors_mentioned": ["sector names"],
  "macro_events": ["event descriptions if any"],
  "is_earnings_related": true/false,
  "is_policy_related": true/false
}}"""

# ============================================================================
# TV TRANSCRIPT EXTRACTION
# ============================================================================

TV_TRANSCRIPT_PROMPT = """Analyze this transcript from an Indian financial TV channel.
Extract stock-specific trading signals, analyst names, and price targets.

Channel: {channel_name}
Timestamp: {timestamp}
Language: {language}

Transcript:
{content}

IMPORTANT:
- TV transcripts are messy. Ignore anchor banter, ads, technical glitches.
- Multiple analysts may speak. Attribute signals to specific analysts when possible.
- Price targets on TV are often mentioned verbally: "target of twenty-four hundred" = 2400
- Hindi numbers: "do hazaar paanch sau" = 2500, "pandrah sau" = 1500

Return JSON:
{{
  "signals": [
    {{
      "stock_symbol": "NSE symbol",
      "company_name": "Full company name",
      "action": "BUY | SELL | HOLD | WATCH",
      "target_price": null or number,
      "stop_loss": null or number,
      "entry_price": null or number,
      "timeframe": "intraday | short_term | medium_term | long_term",
      "confidence": 0.0 to 1.0,
      "reasoning": "One sentence",
      "analyst_name": "Name of analyst if identifiable, else null",
      "analyst_org": "Organization if mentioned, else null"
    }}
  ],
  "market_sentiment": "bullish | bearish | neutral",
  "sectors_discussed": ["sector names"],
  "key_quotes": ["Important verbatim analyst quotes, max 2"]
}}"""

# ============================================================================
# BSE/NSE FILING EXTRACTION
# ============================================================================

FILING_EXTRACTION_PROMPT = """Analyze this corporate filing/announcement from BSE/NSE.
Extract the trading implications.

Company: {company_name} ({symbol})
Filing Type: {filing_type}
Date: {date}

Content:
{content}

Return JSON:
{{
  "signals": [
    {{
      "stock_symbol": "{symbol}",
      "company_name": "{company_name}",
      "action": "BUY | SELL | HOLD | WATCH",
      "target_price": null,
      "stop_loss": null,
      "timeframe": "short_term | medium_term",
      "confidence": 0.0 to 1.0,
      "reasoning": "Impact assessment in one sentence"
    }}
  ],
  "event_type": "earnings | dividend | bonus | split | board_meeting | agm | 
                  rights_issue | buyback | merger | regulatory | other",
  "event_details": {{
    "eps": null or number,
    "revenue_cr": null or number,
    "yoy_growth_pct": null or number,
    "dividend_per_share": null or number,
    "bonus_ratio": null or string,
    "split_ratio": null or string,
    "record_date": null or "YYYY-MM-DD"
  }},
  "sentiment_impact": "positive | negative | neutral",
  "magnitude": "minor | moderate | major"
}}"""

# ============================================================================
# PODCAST EXTRACTION
# ============================================================================

PODCAST_EXTRACTION_PROMPT = """Analyze this transcript from an Indian financial podcast.
Extract specific stock recommendations and market views.

Podcast: {podcast_name}
Episode: {episode_title}
Date: {date}

Transcript:
{content}

Podcasts tend to be more thoughtful and research-driven than TV. Extract:
- Specific stock picks with reasoning
- Sector-level views
- Macro themes and how they affect specific stocks

Return JSON:
{{
  "signals": [
    {{
      "stock_symbol": "NSE symbol",
      "company_name": "Full company name",
      "action": "BUY | SELL | HOLD | WATCH",
      "target_price": null or number,
      "stop_loss": null or number,
      "timeframe": "short_term | medium_term | long_term",
      "confidence": 0.0 to 1.0,
      "reasoning": "One sentence with the core thesis",
      "analyst_name": "Speaker name if identifiable"
    }}
  ],
  "market_outlook": "bullish | bearish | neutral",
  "themes": ["key investment themes discussed"],
  "sectors_favored": ["sectors with positive view"],
  "sectors_avoided": ["sectors with negative view"]
}}"""

# ============================================================================
# TWITTER/X EXTRACTION
# ============================================================================

TWITTER_EXTRACTION_PROMPT = """Analyze this tweet/thread from an Indian financial account.
Extract any specific stock signals.

Author: {author} (@{handle})
Posted: {posted_at}

Tweet:
{content}

Twitter signals are often brief and use shorthand:
- $RELIANCE, $TCS = stock symbols
- "adding", "accumulating" = BUY
- "booking profits", "exiting" = SELL
- "sl hit" = stop loss was triggered
- "tgt done" = target achieved
- Cashtags ($) and hashtags (#) indicate stock mentions

Return JSON:
{{
  "signals": [
    {{
      "stock_symbol": "NSE symbol",
      "action": "BUY | SELL | HOLD | WATCH",
      "target_price": null or number,
      "stop_loss": null or number,
      "confidence": 0.0 to 1.0,
      "reasoning": "One sentence"
    }}
  ],
  "is_original_analysis": true/false,
  "is_retweet_commentary": true/false
}}"""

# ============================================================================
# BATCH EXTRACTION (multiple items in one call for efficiency)
# ============================================================================

BATCH_EXTRACTION_PROMPT = """Analyze these {count} financial news items from Indian markets.
For EACH item, extract trading signals if any exist.

Items:
{items_json}

Return JSON array — one result object per item, in the same order:
[
  {{
    "item_index": 0,
    "signals": [...],
    "market_sentiment": "bullish | bearish | neutral"
  }},
  {{
    "item_index": 1,
    "signals": [...],
    "market_sentiment": "bullish | bearish | neutral"  
  }}
]

Rules:
- Return empty signals array if an item has no specific stock signals
- Keep confidence scores consistent across items (don't inflate)
- Process each item independently — don't let one item's context affect another"""

# ============================================================================
# FINANCIAL KEYWORD SETS (for pre-filtering before LLM)
# ============================================================================

FINANCIAL_KEYWORDS_EN = {
    "buy", "sell", "hold", "target", "stop loss", "resistance", "support",
    "breakout", "breakdown", "bullish", "bearish", "accumulate", "book profit",
    "outperform", "underperform", "overweight", "underweight", "upgrade",
    "downgrade", "quarterly results", "earnings", "dividend", "bonus",
    "split", "ipo", "fpo", "ofs", "nifty", "sensex", "bank nifty",
    "fii", "dii", "mutual fund", "etf", "delivery", "volume",
    "rsi", "macd", "moving average", "pe ratio", "eps",
    "revenue", "profit", "margin", "guidance", "outlook",
    "upper circuit", "lower circuit", "block deal", "bulk deal"
}

FINANCIAL_KEYWORDS_HI = {
    "kharido", "becho", "lakshya", "target", "nifty", "sensex",
    "tezi", "mandi", "munafa", "nuksan", "bazaar", "sharey",
    "stock", "share", "kamai", "aay", "labh", "nivesh",
    "kharid", "bikri", "girta", "badhta", "circuit",
    "support", "resistance", "breakout", "level"
}

ALL_FINANCIAL_KEYWORDS = FINANCIAL_KEYWORDS_EN | FINANCIAL_KEYWORDS_HI
