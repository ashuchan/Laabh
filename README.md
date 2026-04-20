# Laabh — Personal Paper Trading System for Indian Markets

## What is this?

A personal-use system that:
1. **Collects** real-time stock prices from BSE/NSE via Angel One SmartAPI
2. **Scrapes** financial news from Moneycontrol, ET, LiveMint, Google News, BSE/NSE filings
3. **Transcribes** live YouTube financial channels (CNBC-TV18, Zee Business) via Whisper
4. **Extracts** actionable buy/sell signals using Claude AI
5. **Tracks** analyst accuracy with an auto-scoring scoreboard
6. **Paper trades** with virtual capital against real market data
7. **Notifies** you via Telegram when high-confidence signals emerge
8. Wraps it all in a **React Native mobile app**

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        DATA SOURCES                             │
├──────────┬──────────┬──────────┬──────────┬────────────────────┤
│ Angel One│  RSS     │ YouTube  │ BSE/NSE  │ Google News /      │
│ WebSocket│  Feeds   │ Whisper  │ Filings  │ Twitter            │
└────┬─────┴────┬─────┴────┬─────┴────┬─────┴──────┬─────────────┘
     │          │          │          │            │
     ▼          ▼          ▼          ▼            ▼
┌─────────────────────────────────────────────────────────────────┐
│                    INGESTION LAYER                               │
│  Collectors → Dedup (SimHash) → raw_content table               │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                   EXTRACTION LAYER                               │
│  Claude API → Structured JSON → Signal + Analyst matching       │
│  Financial keyword filter (skip 80% noise before LLM call)      │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                  INTELLIGENCE LAYER                              │
│  Convergence scoring │ Technical validation │ Analyst tracking   │
│  Signal auto-trading │ Source credibility   │ Watchlist alerts   │
└──────────────┬───────────────────┬──────────────────────────────┘
               │                   │
               ▼                   ▼
┌──────────────────────┐  ┌───────────────────────────────────────┐
│   PAPER TRADING      │  │         NOTIFICATIONS                 │
│   ENGINE             │  │  Telegram bot │ Push notifications    │
│   Portfolio P&L      │  │  Price alerts │ Signal alerts          │
│   Benchmark compare  │  │  Daily report │ Analyst calls          │
└──────────┬───────────┘  └───────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────────┐
│                    MOBILE APP (React Native)                     │
│  Portfolio │ Signals │ Watchlist │ Trade │ Analysts │ News       │
└─────────────────────────────────────────────────────────────────┘
```

---

## Phased Build Plan

### Phase 1: Data Collection POC (CLAUDE.md)
- Angel One WebSocket integration for live prices
- RSS feed polling (7+ sources)
- Claude API signal extraction
- PostgreSQL + TimescaleDB schema
- Telegram notifications
- **Estimated time: 2-3 weeks**

### Phase 2: Paper Trading Engine (CLAUDE-PHASE2.md)
- Trade execution (market, limit, stop-loss orders)
- Portfolio management with realistic brokerage charges
- Analyst scoreboard with auto-scoring
- Signal resolution (hit target/SL/expired)
- FastAPI REST API
- Meta-paper-trading of all signals
- **Estimated time: 2-3 weeks**

### Phase 3: Whisper Pipeline (CLAUDE-PHASE3.md)
- YouTube live stream recording (yt-dlp)
- Whisper transcription (local GPU)
- Post-market batch VOD processing
- Signal convergence engine
- Technical confirmation (RSI, MACD, SMA)
- Podcast ingestion
- **Estimated time: 2-3 weeks**

### Phase 4: Mobile App & Deployment (CLAUDE-PHASE4.md)
- React Native app (Expo)
- WebSocket real-time price streaming to mobile
- Docker Compose production deployment
- Push notifications
- Tailscale VPN for secure access
- **Estimated time: 3-4 weeks**

---

## How Real-Time Stock Data Works

```
Angel One SmartAPI (WebSocket)
         │
         ▼
┌──────────────────────────┐
│  On market open (9:15):  │
│  1. Authenticate (API    │
│     key + TOTP)          │
│  2. Download instrument  │
│     master file          │
│  3. Map watchlist stocks  │
│     → Angel One tokens   │
│  4. Open WebSocket       │
│  5. Subscribe to tokens  │
└──────────┬───────────────┘
           │ tick every ~1 sec
           ▼
┌──────────────────────────┐
│  On each tick:           │
│  1. Store in price_ticks │
│     (TimescaleDB)        │
│  2. Check price alerts   │
│  3. Update holdings LTP  │
│  4. Check pending orders │
│     (limit/SL)           │
│  5. Push to mobile via   │
│     WebSocket relay      │
└──────────────────────────┘
           │
           ▼ every 5 min
┌──────────────────────────┐
│  Portfolio revaluation:  │
│  1. Recalc all holdings  │
│  2. Update portfolio P&L │
│  3. Compare vs Nifty 50  │
│  4. Broadcast to mobile  │
└──────────────────────────┘
           │
           ▼ at 15:35 IST
┌──────────────────────────┐
│  Market close:           │
│  1. Final snapshot       │
│  2. Store daily OHLCV    │
│  3. Disconnect WebSocket │
│  4. Switch to yfinance   │
│     for after-hours data │
└──────────────────────────┘
```

---

## How Mock Trading Works

1. **Virtual Capital**: Start with ₹10 lakh (configurable). Tracked in `portfolios.current_cash`.

2. **Placing a Trade**: 
   - User selects stock, qty, order type (market/limit/SL)
   - Risk manager checks: enough cash? Position < 10% of portfolio?
   - Market order: executes at current LTP immediately
   - Limit/SL: stored in `pending_orders`, checked against every price tick

3. **Realistic Charges**: Every trade deducts:
   - Brokerage: 0.03% or ₹20 (whichever lower)
   - STT: 0.1% on sell (delivery)
   - Transaction + GST + stamp duty

4. **Holdings**: `holdings` table tracks qty, avg price, current P&L per stock.
   Updated in real-time as prices change.

5. **Benchmark**: Portfolio return compared against Nifty 50 return from the same
   start date. Shows alpha/underperformance.

6. **Signal-Driven Trading**: When a high-confidence signal arrives, user can tap
   "Trade" on the signal card → pre-fills the order form with stock, direction,
   target, and stop-loss from the signal.

---

## How Notifications Work

### Triggers
| Event | Priority | Channel |
|-------|----------|---------|
| Convergence score ≥ 5 on watchlist stock | Critical | Push + Telegram |
| Watchlist stock hits price alert | Critical | Push + Telegram |
| New signal on watchlist stock | High | Telegram |
| Trusted analyst (>0.7 credibility) gives call | High | Telegram |
| Target hit on open trade | High | Push + Telegram |
| Stop-loss hit on open trade | High | Push + Telegram |
| Daily market report | Medium | Telegram |
| General market signal (non-watchlist) | Low | In-app only |

### Watchlist-Focused Analysis
The system prioritizes your watchlist stocks:
- News extraction runs for ALL stocks, but watchlist stocks get:
  - Immediate notification (others batched)
  - Technical confirmation (RSI, MACD check)
  - Full convergence analysis
  - Analyst history for that specific stock
- Price ticks only stream for watchlist stocks (not all 500+)
- You can set per-stock price alerts: "notify me if RELIANCE drops below ₹2,400"
- Toggle news/signal alerts per watchlist item

---

## File Manifest

```
laabh/
├── CLAUDE.md                     # Phase 1 instructions
├── CLAUDE-PHASE2.md              # Phase 2 instructions
├── CLAUDE-PHASE3.md              # Phase 3 instructions
├── CLAUDE-PHASE4.md              # Phase 4 instructions
├── README.md                     # This file
├── .env.example                  # Environment variables template
├── database/
│   ├── schema.sql                # Full PostgreSQL schema (17 tables, views, functions)
│   └── seed.sql                  # Nifty 50 instruments, sources, default watchlist
└── src/
    └── extraction/
        └── prompts.py            # LLM prompt templates for all source types
```

---

## Monthly Running Cost (Personal Use)

| Component | Cost |
|-----------|------|
| Angel One SmartAPI | ₹0 (free with demat account) |
| News (RSS feeds) | ₹0 |
| Claude API (signal extraction) | ~₹350/month (~200 extractions/day) |
| VPS (optional, for 24/7 operation) | ~₹500/month (Hetzner CX22) |
| GPU (Whisper) | Electricity cost on personal GPU |
| Telegram bot | ₹0 |
| **Total** | **~₹850/month** (or ₹0 if using local Llama + local machine) |
