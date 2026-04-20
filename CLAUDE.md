# CLAUDE.md — Phase 1: Data Collection POC

## Project Overview
Laabh is a personal-use paper trading system for Indian stock markets (BSE/NSE).
This phase builds the **data collection backbone**: real-time price feeds, news ingestion,
and AI-powered signal extraction.

## Tech Stack
- **Language**: Python 3.12+
- **Database**: PostgreSQL 16 + TimescaleDB
- **Price Data**: Angel One SmartAPI (WebSocket for real-time ticks), yfinance (fallback)
- **News**: RSS feeds (feedparser), Playwright (article scraping), Google News
- **Signal Extraction**: Anthropic Claude API (claude-sonnet-4-20250514)
- **Task Queue**: APScheduler (lightweight, no Redis needed for POC)
- **Config**: python-dotenv for .env, Pydantic Settings for validation

## Directory Structure
```
laabh/
├── CLAUDE.md                    # This file
├── .env.example                 # Environment variables template
├── pyproject.toml               # Dependencies (use uv or pip)
├── database/
│   ├── schema.sql               # Full production schema (ALREADY EXISTS)
│   ├── seed.sql                 # Seed data (ALREADY EXISTS)
│   └── migrations/              # Future Alembic migrations
├── src/
│   ├── __init__.py
│   ├── config.py                # Pydantic settings, loads .env
│   ├── db.py                    # SQLAlchemy async engine + session
│   ├── models/                  # SQLAlchemy ORM models matching schema.sql
│   │   ├── __init__.py
│   │   ├── instrument.py
│   │   ├── price.py
│   │   ├── source.py
│   │   ├── content.py
│   │   ├── signal.py
│   │   ├── analyst.py
│   │   ├── watchlist.py
│   │   ├── portfolio.py
│   │   ├── trade.py
│   │   └── notification.py
│   ├── collectors/              # Data ingestion modules
│   │   ├── __init__.py
│   │   ├── base.py              # Abstract BaseCollector class
│   │   ├── angel_one.py         # Angel One SmartAPI WebSocket price feed
│   │   ├── yahoo_finance.py     # yfinance fallback for price data
│   │   ├── rss_collector.py     # RSS feed poller (all RSS sources)
│   │   ├── google_news.py       # Google News RSS aggregation
│   │   ├── nse_scraper.py       # NSE website announcements
│   │   ├── bse_scraper.py       # BSE API for corporate filings
│   │   └── article_scraper.py   # Playwright-based full article extraction
│   ├── extraction/              # NLP / LLM signal extraction
│   │   ├── __init__.py
│   │   ├── llm_extractor.py     # Claude API for structured signal extraction
│   │   ├── prompts.py           # Prompt templates for different source types
│   │   ├── dedup.py             # SimHash deduplication
│   │   └── entity_matcher.py    # Match extracted stock names → instrument IDs
│   ├── services/                # Business logic
│   │   ├── __init__.py
│   │   ├── signal_service.py    # Create, update, resolve signals
│   │   ├── price_service.py     # Store and query price data
│   │   └── notification_service.py  # Telegram notifications
│   ├── scheduler.py             # APScheduler job definitions
│   └── main.py                  # Entry point — starts scheduler + WebSocket
├── tests/
│   ├── test_collectors.py
│   ├── test_extraction.py
│   └── test_signals.py
└── scripts/
    ├── init_db.sh               # Create DB, run schema + seed
    ├── test_angel_one.py        # Quick test of Angel One connection
    └── backfill_prices.py       # Backfill historical daily prices via yfinance
```

## Key Design Decisions

### Angel One SmartAPI Integration
- Use the `smartapi-python` package from Angel One
- Authenticate with API key + client ID + TOTP (pyotp for TOTP generation)
- Subscribe to WebSocket feed for instruments in active watchlists
- Store ticks in `price_ticks` table (TimescaleDB hypertable)
- Reconnect automatically on WebSocket drops (exponential backoff)
- Only subscribe to instruments the user is actively watching (not all 500)
- During non-market hours (after 3:30 PM IST), switch to yfinance for EOD data

### Real-Time Price Update Flow
1. On startup, load all watchlist instruments from DB
2. Map instrument symbols → Angel One tokens using their instrument master file
3. Open WebSocket, subscribe to watchlist tokens
4. On each tick: update `price_ticks`, check price alerts in `watchlist_items`
5. If price crosses alert threshold → create notification → send Telegram
6. Every 5 minutes: batch-update `holdings.current_price` and recalc portfolio P&L

### RSS Collection Flow
1. APScheduler runs `rss_collector.collect()` every 5 minutes
2. For each active RSS source in `data_sources`:
   a. Fetch RSS XML via `feedparser`
   b. For each entry: compute SHA-256 hash of (title + link)
   c. Skip if `content_hash` exists in `raw_content` (dedup)
   d. Insert new entries into `raw_content` with `is_processed = false`
3. A separate job processes unprocessed content:
   a. Send title + summary to Claude API with extraction prompt
   b. Parse structured JSON response
   c. Match stock symbols → `instruments` table
   d. Create `signals` entries for any buy/sell recommendations
   e. If signal is for a watchlist stock → create notification

### Signal Extraction Prompt
The LLM extraction must return structured JSON. The prompt should:
- Accept Hindi, English, and Hinglish text
- Extract: stock symbols, action (BUY/SELL/HOLD), target prices, stop losses
- Identify analyst names when present
- Rate confidence 0-1
- Provide one-line reasoning
- Handle ambiguity: "markets look bullish" = no specific signal, skip
- Only return signals with specific stock mentions

### Deduplication
- Use SHA-256 on `title + url` for exact dedup
- Use SimHash on article text for near-duplicate detection (PTI wire rewrites)
- Two articles with SimHash distance < 3 = same story, only process the first

### Error Handling
- All collectors wrap in try/except and log errors to `job_log` table
- Consecutive errors increment `data_sources.consecutive_errors`
- After 5 consecutive errors, set source status to 'error' and notify via Telegram
- Each collector has its own backoff schedule independent of others

## Environment Variables (.env)
```
# Database
DATABASE_URL=postgresql+asyncpg://laabh:laabh@localhost:5432/laabh

# Angel One SmartAPI
ANGEL_ONE_API_KEY=
ANGEL_ONE_CLIENT_ID=
ANGEL_ONE_PASSWORD=
ANGEL_ONE_TOTP_SECRET=

# Anthropic
ANTHROPIC_API_KEY=

# Telegram Notifications
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# General
LOG_LEVEL=INFO
MARKET_OPEN_TIME=09:15
MARKET_CLOSE_TIME=15:30
TIMEZONE=Asia/Kolkata
```

## Dependencies (pyproject.toml)
```
smartapi-python          # Angel One WebSocket + REST
pyotp                    # TOTP generation for Angel One auth
yfinance                 # Fallback price data
feedparser               # RSS parsing
playwright               # Article scraping (headless browser)
anthropic                # Claude API
asyncpg                  # Async PostgreSQL driver
sqlalchemy[asyncio]      # ORM
alembic                  # DB migrations
apscheduler              # Task scheduling
pydantic-settings        # Config validation
python-dotenv            # .env loading
httpx                    # Async HTTP client
simhash                  # Near-duplicate detection
loguru                   # Better logging
tenacity                 # Retry with backoff
pytz                     # Timezone handling
```

## Build & Run Instructions
1. `cp .env.example .env` — fill in credentials
2. `./scripts/init_db.sh` — create DB, run schema + seed
3. `pip install -e .` (or `uv sync`)
4. `playwright install chromium` — install browser for scraping
5. `python -m src.main` — starts the scheduler and WebSocket listener

## Testing
- `pytest tests/test_collectors.py` — test RSS and price collection
- `python scripts/test_angel_one.py` — verify Angel One connectivity
- `python scripts/backfill_prices.py` — populate historical prices for backtesting

## What Success Looks Like
After Phase 1, running `python -m src.main` should:
1. Connect to Angel One WebSocket and stream live ticks for watchlist stocks
2. Poll 7+ RSS feeds every 5 minutes, store new articles
3. Extract stock signals from news via Claude API
4. Store everything in PostgreSQL with proper dedup
5. Send Telegram alerts when watchlist stocks get new signals or hit price alerts
6. Log all job runs to `job_log` for debugging

## Rules for Claude Code
- Use async/await throughout (asyncpg, httpx, async SQLAlchemy)
- Type hints on every function signature
- Docstrings on every class and public method
- Log every collector run with item count and duration
- Never hardcode credentials — always use config/env
- Handle timezone correctly: all timestamps in UTC, display in IST
- Each collector must be independently testable
- Use `tenacity.retry` for all external API calls with exponential backoff
- Keep the extraction prompt in `prompts.py` as a versioned constant
