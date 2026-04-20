# CLAUDE-PHASE4.md — Mobile App & Production Deployment

## Overview
Phase 4 wraps everything into a **React Native mobile app** and a **Docker Compose
production deployment**. The mobile app is the primary interface for monitoring
portfolio, viewing signals, managing watchlists, and executing paper trades.

## Prerequisites
- Phase 1 + 2 + 3 fully functional
- FastAPI backend with all routes implemented (Phase 2)
- Node.js 20+ and React Native CLI installed
- Docker and Docker Compose installed

## Part A: Mobile App

### Tech Stack
- **Framework**: React Native (Expo managed workflow for fast iteration)
- **State**: Zustand (lightweight, no boilerplate)
- **API Client**: TanStack Query (React Query) for caching + real-time updates
- **Charts**: react-native-wagmi-charts (TradingView-style candlestick charts)
- **Navigation**: React Navigation (bottom tabs + stack)
- **Notifications**: expo-notifications (push) + Telegram bot (parallel)
- **WebSocket**: native WebSocket for real-time price streaming from backend

### App Structure
```
mobile/
├── app.json
├── package.json
├── App.tsx
├── src/
│   ├── api/
│   │   ├── client.ts            # Axios instance with base URL, interceptors
│   │   ├── queries/
│   │   │   ├── portfolio.ts     # usePortfolio, useHoldings, usePortfolioHistory
│   │   │   ├── signals.ts       # useActiveSignals, useSignalDetail
│   │   │   ├── instruments.ts   # useInstruments, useInstrumentPrice
│   │   │   ├── watchlist.ts     # useWatchlists, useWatchlistItems
│   │   │   ├── trades.ts        # useTrades, useTradeHistory
│   │   │   └── analysts.ts      # useAnalystLeaderboard
│   │   └── mutations/
│   │       ├── trade.ts         # useExecuteTrade, useCancelOrder
│   │       ├── watchlist.ts     # useAddToWatchlist, useRemoveFromWatchlist
│   │       └── alerts.ts        # useSetPriceAlert
│   ├── stores/
│   │   ├── priceStore.ts        # WebSocket price updates (Zustand)
│   │   ├── notificationStore.ts # In-app notification badge count
│   │   └── settingsStore.ts     # User preferences
│   ├── screens/
│   │   ├── HomeScreen.tsx       # Portfolio summary + market overview
│   │   ├── PortfolioScreen.tsx  # Detailed holdings, P&L chart, benchmark
│   │   ├── SignalsScreen.tsx    # Active signals feed, filtered by watchlist
│   │   ├── TradeScreen.tsx      # Execute buy/sell, order form
│   │   ├── WatchlistScreen.tsx  # Manage watchlists, price alerts
│   │   ├── StockDetailScreen.tsx # Individual stock: chart, signals, news
│   │   ├── AnalystsScreen.tsx   # Analyst leaderboard with stats
│   │   ├── NewsFeedScreen.tsx   # Aggregated news with sentiment tags
│   │   └── SettingsScreen.tsx   # Notifications, data sources, portfolio config
│   ├── components/
│   │   ├── StockCard.tsx        # Compact stock display (price, change, signal count)
│   │   ├── SignalCard.tsx       # Signal with analyst info, convergence badges
│   │   ├── TradeForm.tsx        # Order type selector, quantity, limit price
│   │   ├── PortfolioChart.tsx   # Line chart: portfolio value vs benchmark
│   │   ├── CandlestickChart.tsx # Price chart with volume
│   │   ├── HoldingRow.tsx       # Single holding with P&L
│   │   ├── AnalystRow.tsx       # Analyst name, hit rate, credibility bar
│   │   ├── SentimentBadge.tsx   # Bullish/Bearish/Neutral pill
│   │   ├── ConvergenceMeter.tsx # Visual convergence score (1-5 dots)
│   │   ├── PriceAlert.tsx       # Set above/below alerts inline
│   │   └── NotificationBell.tsx # Badge with unread count
│   ├── hooks/
│   │   ├── useLivePrice.ts      # Subscribe to WebSocket for a symbol
│   │   ├── useMarketStatus.ts   # Is market open? Time to close?
│   │   └── useNotifications.ts  # Register push, handle incoming
│   ├── utils/
│   │   ├── formatters.ts        # ₹ formatting, % formatting, IST dates
│   │   ├── colors.ts            # Green/red for profit/loss, sentiment colors
│   │   └── constants.ts         # API URL, market hours, etc.
│   └── navigation/
│       └── TabNavigator.tsx     # Bottom tabs: Home, Signals, Trade, Watchlist, More
```

### Key Screens

#### Home Screen
- Portfolio card: total value, day P&L (₹ and %), total P&L
- Mini chart: 30-day portfolio value line vs Nifty 50 line
- Market status banner: "Market Open — closes in 2h 15m"
- Top 3 signals today (highest convergence score)
- Watchlist quick view: scrollable row of stock cards with live prices

#### Signals Screen
- Toggle: "All Signals" vs "Watchlist Only"
- Filter chips: BUY, SELL, HOLD | Today, This Week | High Confidence
- Each signal card shows:
  - Stock name + current price + change%
  - Action badge (BUY/SELL) with color
  - Analyst name + credibility score (progress bar)
  - Convergence meter (filled dots)
  - Target and stop-loss prices
  - "Trade" button → navigates to TradeScreen with pre-filled data
  - Expand: full reasoning, source links, technical confirmation

#### Trade Screen
- Stock search (autocomplete from instruments table)
- Order type selector: Market | Limit | Stop Loss
- Quantity input with "% of cash" quick buttons (10%, 25%, 50%, 100%)
- Price input (for limit/SL orders)
- Order summary: total cost, brokerage, available cash after trade
- "Execute Trade" button with confirmation dialog
- If triggered from a signal: pre-fill stock, action, target, SL

#### Watchlist Screen
- Multiple watchlists (tabs at top)
- Each stock shows: LTP, day change%, active signal count, mini sparkline
- Swipe actions: Set Alert, Quick Buy, Remove
- "Add Stock" button with search
- Long press → set target buy/sell price, toggle news/signal alerts

#### Stock Detail Screen
- Header: stock name, LTP, day change, 52-week range
- Candlestick chart (1D, 1W, 1M, 3M, 1Y, 5Y intervals)
- Tabs below chart:
  - Signals: all signals for this stock (active + resolved)
  - News: articles mentioning this stock
  - Analysts: which analysts have covered this stock, their hit rates
  - Fundamentals: P/E, market cap, sector (from instruments table)
- Sticky bottom bar: "BUY" and "SELL" buttons

### WebSocket Real-Time Prices
The backend exposes a WebSocket at `ws://backend:8000/ws/prices`:
```typescript
// priceStore.ts (Zustand)
const ws = new WebSocket('ws://BACKEND_URL/ws/prices');
ws.onmessage = (event) => {
  const { symbol, ltp, change_pct } = JSON.parse(event.data);
  usePriceStore.getState().updatePrice(symbol, ltp, change_pct);
};
// Subscribe to specific symbols:
ws.send(JSON.stringify({ action: 'subscribe', symbols: ['RELIANCE', 'TCS'] }));
```

The backend WebSocket endpoint:
1. Receives subscription requests from mobile client
2. Relays Angel One WebSocket ticks for subscribed symbols
3. Adds current holdings prices for portfolio valuation
4. Sends updates every 1 second (throttled to avoid overwhelming mobile)

### Push Notifications
- Use Expo Push Notifications for critical signals
- Parallel delivery via Telegram (already implemented in Phase 1-3)
- Backend sends push via `expo-server-sdk-python`
- Notification payload includes `screen` and `params` for deep linking:
  ```json
  {"title": "BUY Signal: RELIANCE", "body": "Target ₹2,800 | Convergence: 4/5",
   "data": {"screen": "SignalDetail", "signalId": "uuid"}}
  ```

### Offline Support
- TanStack Query caches last-known data → app works offline with stale data
- Holdings and portfolio value show "last updated X min ago" badge
- Trades queued offline are submitted when connection restores (via mutation retry)

---

## Part B: Docker Deployment

### Docker Architecture
```
docker/
├── docker-compose.yml
├── docker-compose.dev.yml       # Development overrides (hot reload, debug)
├── .env.docker                  # Docker-specific env vars
├── postgres/
│   ├── Dockerfile               # PostgreSQL 16 + TimescaleDB
│   └── init/
│       ├── 01-schema.sql        # Symlink to database/schema.sql
│       └── 02-seed.sql          # Symlink to database/seed.sql
├── backend/
│   ├── Dockerfile
│   └── entrypoint.sh            # Wait for DB, run migrations, start uvicorn
├── whisper/
│   ├── Dockerfile.gpu           # NVIDIA CUDA base image + Whisper
│   └── entrypoint.sh            # Start whisper worker
├── nginx/
│   ├── nginx.conf               # Reverse proxy for API + WebSocket
│   └── Dockerfile
└── watchdog/
    ├── Dockerfile
    └── healthcheck.py           # Pings all services, alerts on failure
```

### docker-compose.yml
```yaml
version: "3.9"

services:
  postgres:
    build: ./docker/postgres
    restart: always
    environment:
      POSTGRES_DB: laabh
      POSTGRES_USER: laabh
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - pgdata:/var/lib/postgresql/data
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U laabh"]
      interval: 10s
      timeout: 5s
      retries: 5

  backend:
    build:
      context: .
      dockerfile: docker/backend/Dockerfile
    restart: always
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      DATABASE_URL: postgresql+asyncpg://laabh:${DB_PASSWORD}@postgres:5432/laabh
      ANGEL_ONE_API_KEY: ${ANGEL_ONE_API_KEY}
      ANGEL_ONE_CLIENT_ID: ${ANGEL_ONE_CLIENT_ID}
      ANGEL_ONE_PASSWORD: ${ANGEL_ONE_PASSWORD}
      ANGEL_ONE_TOTP_SECRET: ${ANGEL_ONE_TOTP_SECRET}
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
      TELEGRAM_BOT_TOKEN: ${TELEGRAM_BOT_TOKEN}
      TELEGRAM_CHAT_ID: ${TELEGRAM_CHAT_ID}
    ports:
      - "8000:8000"
    volumes:
      - whisper_data:/data/whisper

  whisper-worker:
    build:
      context: .
      dockerfile: docker/whisper/Dockerfile.gpu
    restart: always
    depends_on:
      - postgres
      - backend
    environment:
      DATABASE_URL: postgresql+asyncpg://laabh:${DB_PASSWORD}@postgres:5432/laabh
      WHISPER_MODEL: large-v3
      WHISPER_DEVICE: cuda
    volumes:
      - whisper_data:/data/whisper
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]

  watchdog:
    build: ./docker/watchdog
    restart: always
    environment:
      TELEGRAM_BOT_TOKEN: ${TELEGRAM_BOT_TOKEN}
      TELEGRAM_CHAT_ID: ${TELEGRAM_CHAT_ID}
      SERVICES: "backend:8000,postgres:5432"
    depends_on:
      - backend

volumes:
  pgdata:
  whisper_data:
```

### Backend Dockerfile
```dockerfile
FROM python:3.12-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev curl && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir -e .
RUN playwright install --with-deps chromium

COPY . .

EXPOSE 8000
CMD ["uvicorn", "src.api.app:app", "--host", "0.0.0.0", "--port", "8000", 
     "--workers", "1", "--lifespan", "on"]
```

### Whisper Dockerfile
```dockerfile
FROM nvidia/cuda:12.1.1-runtime-ubuntu22.04

RUN apt-get update && apt-get install -y python3 python3-pip ffmpeg curl && \
    rm -rf /var/lib/apt/lists/*

RUN pip3 install openai-whisper torch torchaudio yt-dlp asyncpg psycopg2-binary

WORKDIR /app
COPY src/whisper_pipeline/ /app/whisper_pipeline/

CMD ["python3", "-m", "whisper_pipeline.pipeline"]
```

### Deployment Commands
```bash
# First-time setup:
cp .env.example .env.docker    # Fill in all credentials
docker compose up -d postgres  # Start DB first
docker compose exec postgres psql -U laabh -f /docker-entrypoint-initdb.d/01-schema.sql
docker compose up -d           # Start everything

# Monitor:
docker compose logs -f backend
docker compose logs -f whisper-worker

# Update after code changes:
docker compose build backend
docker compose up -d backend   # Zero-downtime restart

# GPU check:
docker compose exec whisper-worker nvidia-smi
```

### Production Hardening
- PostgreSQL: enable WAL archiving, set `shared_buffers = 256MB`, `max_connections = 50`
- Backend: use gunicorn with uvicorn workers: `gunicorn -w 2 -k uvicorn.workers.UvicornWorker`
- SSL: add Caddy or Certbot for HTTPS if exposing to internet
- Backups: daily pg_dump to local disk + optional S3 upload via Litestream
- Monitoring: watchdog pings every 60s, Telegram on failure

### Network Security (Personal Use)
- Backend only exposed on localhost (127.0.0.1:8000)
- Mobile app connects via Tailscale VPN (free for personal use, zero config)
- No ports exposed to public internet
- PostgreSQL only accessible from Docker network (not host)

## Testing Phase 4
- Mobile: `cd mobile && npx expo start` — test on physical device via Expo Go
- Docker: `docker compose up -d && docker compose ps` — verify all services healthy
- E2E: open mobile app → verify live prices → execute paper trade → verify in DB
- Push: trigger test notification from backend → verify received on phone

## Rules for Claude Code
- All rules from Phase 1-3 still apply
- Mobile app must handle: no network, slow network, WebSocket disconnect gracefully
- All API calls from mobile must use TanStack Query with proper stale/cache times
- UI must feel responsive: optimistic updates for trades (show immediately, confirm async)
- Colors: green for profit/bullish, red for loss/bearish — never reversed
- All prices formatted with ₹ symbol and Indian number system (1,00,000 not 100,000)
- Charts must handle missing data gracefully (weekends, holidays = gaps)
- Docker builds must be reproducible: pin all dependency versions
- Never store secrets in Docker images — always use environment variables
- Whisper container must gracefully handle GPU unavailability (fallback to CPU)
