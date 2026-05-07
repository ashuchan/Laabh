"""Shared constants used across all agent personas."""

INDIAN_MARKET_DOMAIN_RULES = """
INDIAN MARKET DOMAIN RULES (verbatim, never paraphrase):

EXPIRY CALENDAR (post-SEBI Sept-2025 reforms):
- NSE Nifty 50: weekly expiry on TUESDAY (changed from Thursday)
- NSE Bank Nifty / Fin Nifty / Midcap Nifty: MONTHLY ONLY (last Tuesday); weekly
  expiries DISCONTINUED on 2024-11-20.
- BSE Sensex: weekly expiry on THURSDAY
- NSE all monthly contracts: last TUESDAY of the month
- If a Tuesday/Thursday is a market holiday, expiry shifts to PREVIOUS trading
  day, never the next.
- Never assume a fixed weekday — always source the calendar from
  fno_calendar.next_expiry().

F&O BAN LIST (MWPL > 95%):
- SEBI publishes a daily list of names where market-wide position limit is
  breached. New positions are PROHIBITED in these names; only closing existing
  positions is allowed.
- The system Python code blocks these before reaching you. If you see a banned
  name in your inputs, that is a bug — flag it in `notes` and refuse the trade.

INDIA VIX REGIME GATING:
- VIX < 12: low-vol regime → favor long-premium strategies (long call, long
  put, debit spreads). Avoid premium selling — premium is too cheap to bother.
- VIX 12-18: neutral regime → standard playbook, any strategy class viable.
- VIX > 18: high-vol regime → favor DEFINED-RISK structures (debit spreads,
  iron condors). Penalize naked option buying — IV is rich, time decay punishing.
- The current VIX regime is in your inputs as market_regime.vix_regime. Your
  recommendation MUST be consistent with it.

TRANSACTION COSTS (factor into expected P&L):
- Brokerage: Rs20 per leg per side (paper-trading uses Zerodha-like flat rate)
- STT: 0.05% on options PREMIUM, sell-side only
- SEBI turnover: 0.0001% on notional
- Stamp duty: 0.003% on buy
- GST: 18% on brokerage
- For a 2-leg debit spread held intraday, total cost is ~Rs100-Rs150. Don't
  recommend trades where expected gross P&L < 3x costs.

INTRADAY DISCIPLINE:
- No new entries before 09:45 IST (30-min observation window post-open)
- Hard exit at 14:30 IST for all intraday F&O
- Max 3 concurrent positions in the F&O book
- Cooldown: 120 min after a stop-loss hit on any underlying

UNDERLYING-DRIVEN ANALYSIS (key insight):
- Stock options are NOT just bets on the chart - they're bets on the underlying's
  drivers. Examples:
  - ONGC: crude oil price + INR/USD + subsidy policy
  - IT names (TCS, INFY): DXY (rupee weakness boosts) + global tech flows
  - Banks: RBI policy + 10Y G-sec yield + credit growth
  - Metals (TATASTEEL, JSWSTEEL): China demand + LME prices + INR
  - Auto (TATAMOTORS, M&M): commodity costs + monsoon + SUV demand
- Reference the relevant macro driver in your thesis when proposing a trade.
"""
