-- ============================================================================
-- Laabh — Production Database Schema
-- PostgreSQL 15+ with TimescaleDB extension for time-series price data
-- ============================================================================

-- Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";        -- fuzzy text search

-- TimescaleDB is optional — gracefully degrade to plain PostgreSQL when absent
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS "timescaledb";
EXCEPTION WHEN others THEN
    RAISE NOTICE 'TimescaleDB not available — time-series hypertables will be skipped';
END;
$$;

-- ============================================================================
-- 1. ENUMS
-- ============================================================================

CREATE TYPE source_type AS ENUM (
    'rss_feed',
    'web_scraper',
    'api_feed',
    'youtube_live',
    'youtube_vod',
    'podcast',
    'twitter',
    'telegram_channel',
    'bse_filing',
    'nse_announcement',
    'broker_api',
    'manual'
);

CREATE TYPE source_status AS ENUM ('active', 'paused', 'error', 'disabled');

CREATE TYPE signal_action AS ENUM ('BUY', 'SELL', 'HOLD', 'WATCH');

CREATE TYPE signal_timeframe AS ENUM (
    'intraday', 'short_term', 'medium_term', 'long_term'
);
-- intraday: same day | short: 1-7 days | medium: 1-4 weeks | long: 1+ months

CREATE TYPE signal_status AS ENUM (
    'active', 'hit_target', 'hit_stoploss', 'expired', 'cancelled'
);

CREATE TYPE trade_type AS ENUM ('BUY', 'SELL');

CREATE TYPE trade_status AS ENUM ('open', 'closed', 'cancelled');

CREATE TYPE order_type AS ENUM ('MARKET', 'LIMIT', 'STOP_LOSS', 'STOP_LOSS_MARKET');

CREATE TYPE notification_type AS ENUM (
    'signal_alert',
    'price_alert',
    'watchlist_news',
    'trade_executed',
    'target_hit',
    'stoploss_hit',
    'analyst_call',
    'system'
);

CREATE TYPE notification_priority AS ENUM ('low', 'medium', 'high', 'critical');

CREATE TYPE transcription_status AS ENUM (
    'queued', 'downloading', 'transcribing', 'extracting', 'completed', 'failed'
);

CREATE TYPE market_segment AS ENUM ('NSE_EQ', 'BSE_EQ', 'NSE_FO', 'BSE_FO', 'INDEX');

-- ============================================================================
-- 2. INSTRUMENTS (Stocks, Indices, ETFs)
-- ============================================================================

CREATE TABLE instruments (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    symbol              VARCHAR(20) NOT NULL,       -- e.g., RELIANCE, TCS
    exchange            VARCHAR(10) NOT NULL,       -- NSE, BSE
    segment             market_segment NOT NULL DEFAULT 'NSE_EQ',
    isin                VARCHAR(12),                -- unique ISIN code
    company_name        VARCHAR(200) NOT NULL,
    sector              VARCHAR(100),
    industry            VARCHAR(100),
    market_cap_cr       NUMERIC(15,2),              -- in crores
    lot_size            INT DEFAULT 1,
    tick_size            NUMERIC(6,4) DEFAULT 0.05,
    
    -- Broker-specific tokens for WebSocket subscriptions
    angel_one_token     VARCHAR(20),                -- Angel One instrument token
    kite_token          VARCHAR(20),                -- Zerodha Kite token (future use)
    yahoo_symbol        VARCHAR(30),                -- e.g., RELIANCE.NS
    
    is_fno              BOOLEAN DEFAULT FALSE,
    is_index            BOOLEAN DEFAULT FALSE,
    is_active           BOOLEAN DEFAULT TRUE,
    
    metadata            JSONB DEFAULT '{}',         -- flexible extra fields
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    
    UNIQUE(symbol, exchange)
);

CREATE INDEX idx_instruments_symbol ON instruments(symbol);
CREATE INDEX idx_instruments_sector ON instruments(sector);
CREATE INDEX idx_instruments_yahoo ON instruments(yahoo_symbol);
CREATE INDEX idx_instruments_angel ON instruments(angel_one_token);

-- ============================================================================
-- 3. PRICE DATA (TimescaleDB hypertable for efficient time-series storage)
-- ============================================================================

CREATE TABLE price_ticks (
    instrument_id       UUID NOT NULL REFERENCES instruments(id),
    timestamp           TIMESTAMPTZ NOT NULL,
    ltp                 NUMERIC(12,2) NOT NULL,     -- last traded price
    open                NUMERIC(12,2),
    high                NUMERIC(12,2),
    low                 NUMERIC(12,2),
    close               NUMERIC(12,2),
    volume              BIGINT,
    oi                  BIGINT,                     -- open interest (F&O)
    bid_price           NUMERIC(12,2),
    ask_price           NUMERIC(12,2),
    bid_qty             INT,
    ask_qty             INT,
    change_pct          NUMERIC(8,4),
    
    PRIMARY KEY (instrument_id, timestamp)
);

-- Convert to TimescaleDB hypertable (skipped silently if TimescaleDB is absent)
DO $$
BEGIN
    PERFORM create_hypertable('price_ticks', 'timestamp',
        chunk_time_interval => INTERVAL '1 day',
        if_not_exists => TRUE
    );
EXCEPTION WHEN others THEN
    RAISE NOTICE 'TimescaleDB unavailable — price_ticks is a plain table';
END;
$$;

-- Daily OHLCV summary (materialized for fast portfolio queries)
CREATE TABLE price_daily (
    instrument_id       UUID NOT NULL REFERENCES instruments(id),
    date                DATE NOT NULL,
    open                NUMERIC(12,2),
    high                NUMERIC(12,2),
    low                 NUMERIC(12,2),
    close               NUMERIC(12,2),
    volume              BIGINT,
    vwap                NUMERIC(12,2),
    prev_close          NUMERIC(12,2),
    change_pct          NUMERIC(8,4),
    delivery_pct        NUMERIC(6,2),              -- delivery percentage
    
    PRIMARY KEY (instrument_id, date)
);

-- ============================================================================
-- 4. DATA SOURCES — Configurable source registry
-- ============================================================================

CREATE TABLE data_sources (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name                VARCHAR(100) NOT NULL,       -- "Moneycontrol RSS"
    type                source_type NOT NULL,
    status              source_status DEFAULT 'active',
    
    -- Connection config (varies by type)
    config              JSONB NOT NULL DEFAULT '{}',
    /*  Example configs:
        RSS:     {"url": "https://moneycontrol.com/rss/...", "poll_interval_sec": 300}
        API:     {"base_url": "...", "api_key": "...", "endpoint": "/quotes"}
        YouTube: {"channel_id": "...", "stream_mode": "live|vod", "language": "hi"}
        Twitter: {"account_ids": [...], "keywords": ["nifty", "$RELIANCE"]}
        Broker:  {"provider": "angel_one", "api_key": "...", "client_id": "..."}
    */
    
    -- Extraction config — what the LLM/parser should extract
    extraction_schema   JSONB DEFAULT '{}',
    /*  Example: {
        "extract_signals": true,
        "extract_sentiment": true,
        "extract_price_targets": true,
        "extract_analyst_name": true,
        "language_hint": "hi-en",
        "llm_model": "claude-sonnet-4-20250514",
        "custom_prompt_suffix": "Focus on Nifty 50 stocks only"
    } */
    
    -- Scheduling
    poll_interval_sec   INT DEFAULT 300,            -- how often to check
    last_polled_at      TIMESTAMPTZ,
    last_success_at     TIMESTAMPTZ,
    last_error          TEXT,
    consecutive_errors  INT DEFAULT 0,
    
    -- Rate limiting
    rate_limit_rpm      INT DEFAULT 60,             -- requests per minute
    rate_limit_window   TIMESTAMPTZ,
    request_count       INT DEFAULT 0,
    
    -- Stats
    total_items_fetched BIGINT DEFAULT 0,
    total_signals_gen   BIGINT DEFAULT 0,
    
    priority            INT DEFAULT 5,              -- 1=highest, 10=lowest
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_sources_type ON data_sources(type);
CREATE INDEX idx_sources_status ON data_sources(status);

-- ============================================================================
-- 5. RAW CONTENT — Everything ingested before processing
-- ============================================================================

CREATE TABLE raw_content (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_id           UUID NOT NULL REFERENCES data_sources(id),
    
    -- Deduplication
    content_hash        VARCHAR(64) NOT NULL,       -- SHA-256 of content
    external_id         VARCHAR(500),               -- RSS guid, tweet ID, video ID
    
    -- Content
    title               TEXT,
    content_text        TEXT,                        -- full extracted text
    url                 VARCHAR(2000),
    author              VARCHAR(200),
    published_at        TIMESTAMPTZ,
    
    -- Metadata
    language            VARCHAR(10) DEFAULT 'en',   -- en, hi, hi-en (hinglish)
    content_length      INT,
    media_type          VARCHAR(50),                -- article, video, podcast, tweet
    
    -- Processing state
    is_processed        BOOLEAN DEFAULT FALSE,
    processed_at        TIMESTAMPTZ,
    processing_error    TEXT,
    
    -- LLM extraction results (raw JSON from the model)
    extraction_result   JSONB,
    extraction_model    VARCHAR(50),                -- which model was used
    extraction_tokens   INT,                        -- tokens consumed
    extraction_cost_usd NUMERIC(8,6),               -- cost tracking
    
    -- Similarity for dedup
    simhash             BIGINT,                     -- SimHash for near-duplicate detection
    
    fetched_at          TIMESTAMPTZ DEFAULT NOW(),
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    
    UNIQUE(content_hash)
);

CREATE INDEX idx_raw_content_source ON raw_content(source_id);
CREATE INDEX idx_raw_content_processed ON raw_content(is_processed);
CREATE INDEX idx_raw_content_published ON raw_content(published_at DESC);
CREATE INDEX idx_raw_content_hash ON raw_content(content_hash);
CREATE INDEX idx_raw_content_simhash ON raw_content(simhash);
CREATE INDEX idx_raw_content_external ON raw_content(external_id);

-- ============================================================================
-- 6. ANALYSTS — defined before signals because signals.analyst_id FKs here
-- ============================================================================

CREATE TABLE analysts (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name                VARCHAR(200) NOT NULL,
    normalized_name     VARCHAR(200) NOT NULL,       -- lowercase, trimmed

    -- Affiliations
    organization        VARCHAR(200),               -- "CNBC-TV18", "ICICI Securities"
    designation         VARCHAR(200),               -- "Market Analyst", "Fund Manager"

    -- Sources where this analyst appears
    primary_source_ids  UUID[],

    -- Performance scoreboard (updated nightly by cron)
    total_signals       INT DEFAULT 0,
    signals_hit_target  INT DEFAULT 0,
    signals_hit_sl      INT DEFAULT 0,
    signals_expired     INT DEFAULT 0,
    hit_rate            NUMERIC(5,4) DEFAULT 0,
    avg_return_pct      NUMERIC(8,4) DEFAULT 0,
    avg_days_to_target  NUMERIC(6,1),
    best_sector         VARCHAR(100),

    -- Credibility score (composite)
    credibility_score   NUMERIC(5,3) DEFAULT 0.5,

    -- Metadata
    notes               TEXT,
    metadata            JSONB DEFAULT '{}',
    is_active           BOOLEAN DEFAULT TRUE,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),

    UNIQUE(normalized_name, organization)
);

CREATE INDEX idx_analysts_name ON analysts USING gin(normalized_name gin_trgm_ops);
CREATE INDEX idx_analysts_credibility ON analysts(credibility_score DESC);
CREATE INDEX idx_analysts_hit_rate ON analysts(hit_rate DESC);

-- ============================================================================
-- 7. SIGNALS — Extracted buy/sell/hold recommendations
-- ============================================================================

CREATE TABLE signals (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    content_id          UUID REFERENCES raw_content(id),
    instrument_id       UUID NOT NULL REFERENCES instruments(id),
    source_id           UUID NOT NULL REFERENCES data_sources(id),
    
    -- Signal details
    action              signal_action NOT NULL,
    timeframe           signal_timeframe DEFAULT 'short_term',
    
    -- Prices at signal time
    entry_price         NUMERIC(12,2),              -- suggested entry
    target_price        NUMERIC(12,2),              -- target
    stop_loss           NUMERIC(12,2),              -- stop loss
    current_price_at_signal NUMERIC(12,2),          -- market price when signal generated
    
    -- Confidence & reasoning
    confidence          NUMERIC(4,3) CHECK (confidence BETWEEN 0 AND 1),
    reasoning           TEXT,
    
    -- Analyst attribution
    analyst_id          UUID REFERENCES analysts(id),
    analyst_name_raw    VARCHAR(200),               -- raw name from extraction
    
    -- Convergence
    convergence_score   INT DEFAULT 1,              -- how many sources agree
    related_signal_ids  UUID[],                     -- other signals on same stock/direction
    
    -- Outcome tracking
    status              signal_status DEFAULT 'active',
    outcome_price       NUMERIC(12,2),
    outcome_date        TIMESTAMPTZ,
    outcome_pnl_pct     NUMERIC(8,4),              -- actual P&L if followed
    days_to_outcome     INT,
    
    -- Timestamps
    signal_date         TIMESTAMPTZ DEFAULT NOW(),
    expiry_date         TIMESTAMPTZ,                -- when signal expires
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_signals_instrument ON signals(instrument_id);
CREATE INDEX idx_signals_action ON signals(action);
CREATE INDEX idx_signals_status ON signals(status);
CREATE INDEX idx_signals_date ON signals(signal_date DESC);
CREATE INDEX idx_signals_analyst ON signals(analyst_id);
CREATE INDEX idx_signals_convergence ON signals(convergence_score DESC);

-- ============================================================================
-- 8. WATCHLISTS — User's focused stock lists
-- ============================================================================

CREATE TABLE watchlists (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name                VARCHAR(100) NOT NULL,       -- "Core Portfolio", "Swing Trades"
    description         TEXT,
    is_default          BOOLEAN DEFAULT FALSE,       -- one default watchlist
    sort_order          INT DEFAULT 0,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE watchlist_items (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    watchlist_id        UUID NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
    instrument_id       UUID NOT NULL REFERENCES instruments(id),
    
    -- Alerts
    price_alert_above   NUMERIC(12,2),
    price_alert_below   NUMERIC(12,2),
    alert_on_news       BOOLEAN DEFAULT TRUE,
    alert_on_signals    BOOLEAN DEFAULT TRUE,
    
    -- Notes
    notes               TEXT,
    target_buy_price    NUMERIC(12,2),              -- your personal target
    target_sell_price   NUMERIC(12,2),
    
    added_at            TIMESTAMPTZ DEFAULT NOW(),
    
    UNIQUE(watchlist_id, instrument_id)
);

CREATE INDEX idx_watchlist_items_instrument ON watchlist_items(instrument_id);

-- ============================================================================
-- 9. PAPER TRADING — Virtual portfolio and trades
-- ============================================================================

CREATE TABLE portfolios (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name                VARCHAR(100) NOT NULL DEFAULT 'Main Portfolio',
    initial_capital     NUMERIC(15,2) NOT NULL DEFAULT 1000000,  -- ₹10 lakh
    current_cash        NUMERIC(15,2) NOT NULL DEFAULT 1000000,
    
    -- Performance (updated in real-time)
    invested_value      NUMERIC(15,2) DEFAULT 0,
    current_value       NUMERIC(15,2) DEFAULT 0,
    total_pnl           NUMERIC(15,2) DEFAULT 0,
    total_pnl_pct       NUMERIC(8,4) DEFAULT 0,
    day_pnl             NUMERIC(15,2) DEFAULT 0,
    
    -- Benchmark comparison
    benchmark_symbol    VARCHAR(20) DEFAULT 'NIFTY 50',
    benchmark_start     NUMERIC(12,2),              -- benchmark value at portfolio start
    
    -- Stats
    total_trades        INT DEFAULT 0,
    winning_trades      INT DEFAULT 0,
    losing_trades       INT DEFAULT 0,
    win_rate            NUMERIC(5,4) DEFAULT 0,
    max_drawdown_pct    NUMERIC(8,4) DEFAULT 0,
    sharpe_ratio        NUMERIC(6,4),
    
    -- Brokerage simulation
    brokerage_pct       NUMERIC(6,4) DEFAULT 0.0003, -- 0.03% per trade
    stt_pct             NUMERIC(6,4) DEFAULT 0.001,  -- STT for delivery
    
    is_active           BOOLEAN DEFAULT TRUE,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE trades (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    portfolio_id        UUID NOT NULL REFERENCES portfolios(id),
    instrument_id       UUID NOT NULL REFERENCES instruments(id),
    signal_id           UUID REFERENCES signals(id), -- which signal triggered this trade
    
    -- Order details
    trade_type          trade_type NOT NULL,
    order_type          order_type NOT NULL DEFAULT 'MARKET',
    quantity            INT NOT NULL,
    price               NUMERIC(12,2) NOT NULL,      -- execution price
    limit_price         NUMERIC(12,2),               -- for limit orders
    trigger_price       NUMERIC(12,2),               -- for stop-loss orders
    
    -- Costs
    brokerage           NUMERIC(10,2) DEFAULT 0,
    stt                 NUMERIC(10,2) DEFAULT 0,
    total_cost          NUMERIC(12,2),               -- price * qty + charges
    
    -- Status
    status              trade_status DEFAULT 'open',
    
    -- For closed trades: link to closing trade
    closing_trade_id    UUID REFERENCES trades(id),
    pnl                 NUMERIC(12,2),               -- realized P&L
    pnl_pct             NUMERIC(8,4),
    holding_days        INT,
    
    -- Journal
    entry_reason        TEXT,                        -- why did you take this trade
    exit_reason         TEXT,
    lessons_learned     TEXT,
    
    executed_at         TIMESTAMPTZ DEFAULT NOW(),
    closed_at           TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_trades_portfolio ON trades(portfolio_id);
CREATE INDEX idx_trades_instrument ON trades(instrument_id);
CREATE INDEX idx_trades_status ON trades(status);
CREATE INDEX idx_trades_executed ON trades(executed_at DESC);
CREATE INDEX idx_trades_signal ON trades(signal_id);

-- Current holdings (materialized view, refreshed on each trade)
CREATE TABLE holdings (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    portfolio_id        UUID NOT NULL REFERENCES portfolios(id),
    instrument_id       UUID NOT NULL REFERENCES instruments(id),
    
    quantity            INT NOT NULL,
    avg_buy_price       NUMERIC(12,2) NOT NULL,
    invested_value      NUMERIC(15,2) NOT NULL,
    current_price       NUMERIC(12,2),
    current_value       NUMERIC(15,2),
    pnl                 NUMERIC(15,2),
    pnl_pct             NUMERIC(8,4),
    day_change_pct      NUMERIC(8,4),
    weight_pct          NUMERIC(6,2),               -- % of portfolio
    
    first_buy_date      TIMESTAMPTZ,
    last_trade_date     TIMESTAMPTZ,
    
    UNIQUE(portfolio_id, instrument_id)
);

CREATE INDEX idx_holdings_portfolio ON holdings(portfolio_id);

-- ============================================================================
-- 9b. PENDING ORDERS — Limit and stop-loss orders waiting for price triggers
-- ============================================================================

CREATE TABLE pending_orders (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    portfolio_id    UUID NOT NULL REFERENCES portfolios(id),
    instrument_id   UUID NOT NULL REFERENCES instruments(id),
    signal_id       UUID REFERENCES signals(id),
    trade_type      trade_type NOT NULL,
    order_type      order_type NOT NULL,
    quantity        INT NOT NULL,
    limit_price     NUMERIC(12,2),
    trigger_price   NUMERIC(12,2),
    status          VARCHAR(20) DEFAULT 'pending',  -- pending, executed, cancelled, expired
    valid_till      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    executed_at     TIMESTAMPTZ,
    cancelled_at    TIMESTAMPTZ
);

CREATE INDEX idx_pending_orders_portfolio ON pending_orders(portfolio_id);
CREATE INDEX idx_pending_orders_status ON pending_orders(status);

-- ============================================================================
-- 10. PORTFOLIO SNAPSHOTS — Daily NAV history for charting
-- ============================================================================

CREATE TABLE portfolio_snapshots (
    portfolio_id        UUID NOT NULL REFERENCES portfolios(id),
    date                DATE NOT NULL,
    total_value         NUMERIC(15,2),
    cash                NUMERIC(15,2),
    invested_value      NUMERIC(15,2),
    day_pnl             NUMERIC(15,2),
    day_pnl_pct         NUMERIC(8,4),
    cumulative_pnl_pct  NUMERIC(8,4),
    benchmark_value     NUMERIC(12,2),              -- benchmark on same date
    benchmark_pnl_pct   NUMERIC(8,4),
    num_holdings        INT,
    
    PRIMARY KEY (portfolio_id, date)
);

-- ============================================================================
-- 10b. EQUITY STRATEGY — LLM-driven paper trading decisions
-- ============================================================================
-- Each decision_type captures one LLM invocation:
--   morning_allocation     — 09:10 IST: split daily budget across BUY signals
--   intraday_action        — 09:30–14:30 IST: hold/sell/buy based on monitoring
--   eod_squareoff          — 15:20 IST: which intraday positions to close
-- input_summary captures candidates/holdings shown to LLM (for replay & audit).
-- actions_json captures the LLM's structured output (allocations or trade list).
-- One row per call; trades reference this via entry_reason or a follow-up join.

CREATE TABLE strategy_decisions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    portfolio_id        UUID NOT NULL REFERENCES portfolios(id),
    decision_type       VARCHAR(40) NOT NULL,          -- morning_allocation | intraday_action | eod_squareoff
    as_of               TIMESTAMPTZ NOT NULL,          -- logical decision time (live = now, replay = historical)
    risk_profile        VARCHAR(20),                   -- safe | balanced | aggressive
    budget_available    NUMERIC(15,2),                 -- cash visible to LLM at decision time
    input_summary       JSONB,                         -- candidates, holdings, signals snapshot
    llm_model           VARCHAR(80),
    llm_reasoning       TEXT,                          -- LLM's freeform explanation
    actions_json        JSONB NOT NULL,                -- structured action list returned by LLM
    actions_executed    INT DEFAULT 0,                 -- count of actions that successfully reached engine
    actions_skipped     INT DEFAULT 0,                 -- count rejected by risk/cash/missing-LTP checks
    dryrun_run_id       UUID,                          -- non-null when invoked from src/dryrun
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_strategy_decisions_portfolio ON strategy_decisions(portfolio_id, as_of DESC);
CREATE INDEX idx_strategy_decisions_type ON strategy_decisions(decision_type, as_of DESC);
CREATE INDEX idx_strategy_decisions_dryrun ON strategy_decisions(dryrun_run_id) WHERE dryrun_run_id IS NOT NULL;

-- ============================================================================
-- 11. NOTIFICATIONS
-- ============================================================================

CREATE TABLE notifications (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    type                notification_type NOT NULL,
    priority            notification_priority DEFAULT 'medium',
    
    title               VARCHAR(200) NOT NULL,
    body                TEXT NOT NULL,
    
    -- References
    instrument_id       UUID REFERENCES instruments(id),
    signal_id           UUID REFERENCES signals(id),
    trade_id            UUID REFERENCES trades(id),
    
    -- Delivery
    is_read             BOOLEAN DEFAULT FALSE,
    is_pushed           BOOLEAN DEFAULT FALSE,       -- sent to phone
    pushed_at           TIMESTAMPTZ,
    push_channel        VARCHAR(50),                 -- 'telegram', 'push_notification', 'email'
    
    -- Action
    action_url          VARCHAR(500),
    action_data         JSONB,                       -- e.g., {"screen": "signal_detail", "id": "..."}
    
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_notifications_unread ON notifications(is_read) WHERE NOT is_read;
CREATE INDEX idx_notifications_created ON notifications(created_at DESC);
CREATE INDEX idx_notifications_type ON notifications(type);
CREATE INDEX idx_notifications_instrument ON notifications(instrument_id);

-- ============================================================================
-- 12. WHISPER PIPELINE — Audio transcription jobs
-- ============================================================================

CREATE TABLE transcription_jobs (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_id           UUID NOT NULL REFERENCES data_sources(id),
    
    -- Source media
    video_id            VARCHAR(20),                 -- YouTube video ID
    video_title         VARCHAR(500),
    channel_name        VARCHAR(200),
    stream_url          VARCHAR(2000),
    
    -- Audio
    audio_file_path     VARCHAR(500),
    audio_duration_sec  INT,
    audio_format        VARCHAR(10) DEFAULT 'webm',
    
    -- Processing
    status              transcription_status DEFAULT 'queued',
    whisper_model       VARCHAR(20) DEFAULT 'large-v3',
    language_detected   VARCHAR(10),
    
    -- Output
    transcript_text     TEXT,
    transcript_segments JSONB,                       -- [{start, end, text}, ...]
    word_count          INT,
    
    -- Extraction
    signals_extracted   INT DEFAULT 0,
    extraction_result   JSONB,
    
    -- Timing
    download_started_at TIMESTAMPTZ,
    transcription_started_at TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    processing_time_sec INT,
    
    -- Errors
    error_message       TEXT,
    retry_count         INT DEFAULT 0,
    max_retries         INT DEFAULT 3,
    
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_transcription_status ON transcription_jobs(status);
CREATE INDEX idx_transcription_source ON transcription_jobs(source_id);
CREATE INDEX idx_transcription_video ON transcription_jobs(video_id);

-- Transcript chunks — for long videos, store segments separately
CREATE TABLE transcript_chunks (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id              UUID NOT NULL REFERENCES transcription_jobs(id) ON DELETE CASCADE,
    
    chunk_index         INT NOT NULL,
    start_time_sec      NUMERIC(10,2),
    end_time_sec        NUMERIC(10,2),
    text                TEXT NOT NULL,
    
    -- NLP results per chunk
    contains_stock_mention BOOLEAN DEFAULT FALSE,
    stock_symbols       VARCHAR(20)[],
    sentiment           VARCHAR(10),                 -- bullish, bearish, neutral
    extraction_result   JSONB,
    
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_chunks_job ON transcript_chunks(job_id);
CREATE INDEX idx_chunks_stocks ON transcript_chunks USING gin(stock_symbols);

-- ============================================================================
-- 13. MARKET SENTIMENT — Aggregated market-level signals
-- ============================================================================

CREATE TABLE market_sentiment (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    timestamp           TIMESTAMPTZ NOT NULL,
    
    -- Overall
    overall_sentiment   VARCHAR(10),                 -- bullish, bearish, neutral
    sentiment_score     NUMERIC(4,3),                -- -1 to +1
    
    -- By source type
    news_sentiment      NUMERIC(4,3),
    tv_sentiment        NUMERIC(4,3),
    twitter_sentiment   NUMERIC(4,3),
    
    -- Market stats at this point
    nifty_value         NUMERIC(12,2),
    nifty_change_pct    NUMERIC(8,4),
    advance_decline     NUMERIC(6,2),                -- advance/decline ratio
    fii_flow_cr         NUMERIC(12,2),               -- FII buy/sell in crores
    dii_flow_cr         NUMERIC(12,2),
    
    -- Sector heatmap
    sector_sentiment    JSONB,                       -- {"IT": 0.7, "Banks": -0.3, ...}
    
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_sentiment_time ON market_sentiment(timestamp DESC);

-- ============================================================================
-- 14. SYSTEM — Job queue, audit log, config
-- ============================================================================

CREATE TABLE system_config (
    key                 VARCHAR(100) PRIMARY KEY,
    value               JSONB NOT NULL,
    description         TEXT,
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- Insert default config
INSERT INTO system_config (key, value, description) VALUES
('angel_one', '{"api_key": "", "client_id": "", "password": "", "totp_secret": ""}', 'Angel One SmartAPI credentials'),
('llm_config', '{"provider": "anthropic", "model": "claude-sonnet-4-20250514", "api_key": "", "max_tokens": 2000}', 'LLM configuration for signal extraction'),
('whisper_config', '{"model": "large-v3", "device": "cuda", "language": "hi", "chunk_duration_sec": 60}', 'Whisper transcription settings'),
('notification_config', '{"telegram_bot_token": "", "telegram_chat_id": "", "enabled": true}', 'Notification delivery settings'),
('trading_config', '{"default_capital": 1000000, "brokerage_pct": 0.0003, "max_position_pct": 10}', 'Paper trading defaults'),
('scraping_config', '{"user_agent": "Mozilla/5.0 Laabh/1.0", "request_delay_ms": 2000, "max_concurrent": 3}', 'Scraping behavior settings');

CREATE TABLE job_log (
    id                  BIGSERIAL PRIMARY KEY,
    job_name            VARCHAR(100) NOT NULL,
    source_id           UUID REFERENCES data_sources(id),
    status              VARCHAR(20) NOT NULL,        -- started, completed, failed
    items_processed     INT DEFAULT 0,
    signals_generated   INT DEFAULT 0,
    duration_ms         INT,
    error_message       TEXT,
    metadata            JSONB,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_job_log_name ON job_log(job_name);
CREATE INDEX idx_job_log_created ON job_log(created_at DESC);

-- LLM audit log — every Claude API call across the system writes one row here
CREATE TABLE llm_audit_log (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    caller          VARCHAR(50) NOT NULL,       -- "phase1.extractor" / "fno.thesis" / etc.
    caller_ref_id   UUID,                       -- FK to caller's row (raw_content.id, etc.)
    model           VARCHAR(50) NOT NULL,
    temperature     NUMERIC(4,2) NOT NULL,
    prompt          TEXT NOT NULL,
    response        TEXT NOT NULL,
    response_parsed JSONB,
    tokens_in       INT,
    tokens_out      INT,
    latency_ms      INT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_llm_audit_caller ON llm_audit_log(caller, created_at DESC);
CREATE INDEX idx_llm_audit_caller_ref ON llm_audit_log(caller_ref_id);

-- ============================================================================
-- 15. META-PAPER-TRADING — Auto-trade every signal to evaluate source quality
-- ============================================================================

CREATE TABLE signal_auto_trades (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    signal_id           UUID NOT NULL REFERENCES signals(id),
    instrument_id       UUID NOT NULL REFERENCES instruments(id),
    source_id           UUID NOT NULL REFERENCES data_sources(id),
    analyst_id          UUID REFERENCES analysts(id),
    
    -- Virtual trade
    action              signal_action NOT NULL,
    entry_price         NUMERIC(12,2) NOT NULL,
    target_price        NUMERIC(12,2),
    stop_loss           NUMERIC(12,2),
    
    -- Outcome
    status              signal_status DEFAULT 'active',
    exit_price          NUMERIC(12,2),
    pnl_pct             NUMERIC(8,4),
    days_held           INT,
    resolved_at         TIMESTAMPTZ,
    
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_auto_trades_source ON signal_auto_trades(source_id);
CREATE INDEX idx_auto_trades_analyst ON signal_auto_trades(analyst_id);
CREATE INDEX idx_auto_trades_status ON signal_auto_trades(status);

-- ============================================================================
-- F&O INTELLIGENCE MODULE — Tables added in Phase F&O
-- ============================================================================

-- Daily F&O ban list (SEBI MWPL>95%)
CREATE TABLE fno_ban_list (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    instrument_id   UUID NOT NULL REFERENCES instruments(id),
    ban_date        DATE NOT NULL,
    source          VARCHAR(20) DEFAULT 'NSE',
    fetched_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (instrument_id, ban_date, source)
);
CREATE INDEX idx_fno_ban_date ON fno_ban_list(ban_date);

-- Options chain snapshots
CREATE TABLE options_chain (
    instrument_id     UUID NOT NULL REFERENCES instruments(id),
    snapshot_at       TIMESTAMPTZ NOT NULL,
    expiry_date       DATE NOT NULL,
    strike_price      NUMERIC(12,2) NOT NULL,
    option_type       CHAR(2) NOT NULL CHECK (option_type IN ('CE','PE')),
    ltp               NUMERIC(12,2),
    bid_price         NUMERIC(12,2),
    ask_price         NUMERIC(12,2),
    bid_qty           INT,
    ask_qty           INT,
    volume            BIGINT,
    oi                BIGINT,
    oi_change         BIGINT,
    iv                NUMERIC(8,4),
    delta             NUMERIC(8,4),
    gamma             NUMERIC(10,6),
    theta             NUMERIC(10,4),
    vega              NUMERIC(10,4),
    underlying_ltp    NUMERIC(12,2),
    PRIMARY KEY (instrument_id, snapshot_at, expiry_date, strike_price, option_type)
);
DO $$
BEGIN
    PERFORM create_hypertable('options_chain', 'snapshot_at',
        chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
EXCEPTION WHEN others THEN
    RAISE NOTICE 'TimescaleDB unavailable — options_chain is a plain table';
END;
$$;
CREATE INDEX idx_options_chain_underlying_expiry
    ON options_chain(instrument_id, expiry_date, snapshot_at DESC);

-- Daily IV percentile per instrument
CREATE TABLE iv_history (
    instrument_id     UUID NOT NULL REFERENCES instruments(id),
    date              DATE NOT NULL,
    atm_iv            NUMERIC(8,4) NOT NULL,
    iv_rank_52w       NUMERIC(6,2),
    iv_percentile_52w NUMERIC(6,2),
    PRIMARY KEY (instrument_id, date)
);

-- India VIX time series
CREATE TABLE vix_ticks (
    timestamp         TIMESTAMPTZ NOT NULL,
    vix_value         NUMERIC(8,4) NOT NULL,
    regime            VARCHAR(10) NOT NULL CHECK (regime IN ('low','neutral','high')),
    PRIMARY KEY (timestamp)
);
DO $$
BEGIN
    PERFORM create_hypertable('vix_ticks', 'timestamp',
        chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE);
EXCEPTION WHEN others THEN
    RAISE NOTICE 'TimescaleDB unavailable — vix_ticks is a plain table';
END;
$$;

-- Phase 1/2/3 candidate snapshots
CREATE TABLE fno_candidates (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    instrument_id     UUID NOT NULL REFERENCES instruments(id),
    run_date          DATE NOT NULL,
    phase             INT NOT NULL CHECK (phase IN (1,2,3)),
    passed_liquidity  BOOLEAN,
    atm_oi            BIGINT,
    atm_spread_pct    NUMERIC(6,4),
    avg_volume_5d     BIGINT,
    news_score        NUMERIC(4,2),
    sentiment_score   NUMERIC(4,2),
    fii_dii_score     NUMERIC(4,2),
    macro_align_score NUMERIC(4,2),
    convergence_score NUMERIC(4,2),
    composite_score   NUMERIC(6,2),
    technical_pass    BOOLEAN,
    iv_regime         VARCHAR(15),
    oi_structure      VARCHAR(20),
    llm_thesis        TEXT,
    llm_decision      VARCHAR(10),
    config_version    VARCHAR(20),
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (instrument_id, run_date, phase)
);
CREATE INDEX idx_fno_candidates_run ON fno_candidates(run_date, phase, composite_score DESC);

-- F&O signals — strike-level recommendations
CREATE TABLE fno_signals (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    underlying_id       UUID NOT NULL REFERENCES instruments(id),
    candidate_id        UUID REFERENCES fno_candidates(id),
    strategy_type       VARCHAR(20) NOT NULL,
    expiry_date         DATE NOT NULL,
    legs                JSONB NOT NULL,
    entry_premium_net   NUMERIC(12,2),
    target_premium_net  NUMERIC(12,2),
    stop_premium_net    NUMERIC(12,2),
    max_loss            NUMERIC(12,2),
    max_profit          NUMERIC(12,2),
    breakeven_price     NUMERIC(12,2),
    ranker_score        NUMERIC(6,2),
    ranker_breakdown    JSONB,
    ranker_version      VARCHAR(20),
    iv_regime_at_entry  VARCHAR(15),
    vix_at_entry        NUMERIC(8,4),
    status              VARCHAR(15) DEFAULT 'proposed',
    proposed_at         TIMESTAMPTZ DEFAULT NOW(),
    filled_at           TIMESTAMPTZ,
    closed_at           TIMESTAMPTZ,
    final_pnl           NUMERIC(12,2),
    notes               TEXT
);
CREATE INDEX idx_fno_signals_status ON fno_signals(status);
CREATE INDEX idx_fno_signals_underlying ON fno_signals(underlying_id, proposed_at DESC);

-- F&O signal state-change events (append-only audit trail)
CREATE TABLE fno_signal_events (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    signal_id       UUID NOT NULL REFERENCES fno_signals(id),
    from_status     VARCHAR(15),
    to_status       VARCHAR(15) NOT NULL,
    reason          TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_fno_signal_events_signal ON fno_signal_events(signal_id, created_at DESC);

-- Strike ranker config history
CREATE TABLE ranker_configs (
    version           VARCHAR(20) PRIMARY KEY,
    weights           JSONB NOT NULL,
    activated_at      TIMESTAMPTZ DEFAULT NOW(),
    deactivated_at    TIMESTAMPTZ,
    notes             TEXT
);

-- F&O cooldown tracker (revenge-trade prevention)
CREATE TABLE fno_cooldowns (
    underlying_id     UUID NOT NULL REFERENCES instruments(id),
    cooldown_until    TIMESTAMPTZ NOT NULL,
    reason            VARCHAR(50),
    PRIMARY KEY (underlying_id, cooldown_until)
);

-- ============================================================================
-- 16. VIEWS — Useful pre-built queries
-- ============================================================================

-- Analyst leaderboard
CREATE VIEW analyst_leaderboard AS
SELECT 
    a.id,
    a.name,
    a.organization,
    a.total_signals,
    a.signals_hit_target,
    a.hit_rate,
    a.avg_return_pct,
    a.credibility_score,
    a.avg_days_to_target,
    a.best_sector,
    COUNT(DISTINCT s.instrument_id) as stocks_covered,
    MAX(s.signal_date) as last_signal_date
FROM analysts a
LEFT JOIN signals s ON s.analyst_id = a.id
WHERE a.total_signals >= 5
GROUP BY a.id
ORDER BY a.credibility_score DESC;

-- Active signals with instrument details
CREATE VIEW active_signals_view AS
SELECT 
    s.*,
    i.symbol,
    i.company_name,
    i.sector,
    a.name as analyst_name,
    a.hit_rate as analyst_hit_rate,
    a.credibility_score as analyst_credibility,
    ds.name as source_name,
    ds.type as source_type
FROM signals s
JOIN instruments i ON s.instrument_id = i.id
LEFT JOIN analysts a ON s.analyst_id = a.id
JOIN data_sources ds ON s.source_id = ds.id
WHERE s.status = 'active'
ORDER BY s.confidence DESC, s.convergence_score DESC;

-- Portfolio summary with holdings
CREATE VIEW portfolio_overview AS
SELECT 
    p.*,
    COALESCE(SUM(h.current_value), 0) as holdings_value,
    COUNT(h.id) as num_holdings,
    p.current_cash + COALESCE(SUM(h.current_value), 0) as total_value
FROM portfolios p
LEFT JOIN holdings h ON h.portfolio_id = p.id
WHERE p.is_active = TRUE
GROUP BY p.id;

-- Watchlist with live data
CREATE VIEW watchlist_live AS
SELECT 
    wi.*,
    w.name as watchlist_name,
    i.symbol,
    i.company_name,
    i.sector,
    pd.close as last_close,
    pd.change_pct,
    pd.volume,
    (SELECT COUNT(*) FROM signals s 
     WHERE s.instrument_id = wi.instrument_id 
     AND s.status = 'active') as active_signals
FROM watchlist_items wi
JOIN watchlists w ON wi.watchlist_id = w.id
JOIN instruments i ON wi.instrument_id = i.id
LEFT JOIN price_daily pd ON pd.instrument_id = i.id 
    AND pd.date = CURRENT_DATE
ORDER BY w.sort_order, wi.added_at;

-- ============================================================================
-- 17. FUNCTIONS — Useful stored procedures
-- ============================================================================

-- Update analyst scoreboard (run nightly)
CREATE OR REPLACE FUNCTION update_analyst_scores()
RETURNS void AS $$
BEGIN
    UPDATE analysts a SET
        total_signals = sub.total,
        signals_hit_target = sub.hits,
        signals_hit_sl = sub.stops,
        signals_expired = sub.expired,
        hit_rate = CASE WHEN sub.resolved > 0 
                   THEN sub.hits::NUMERIC / sub.resolved ELSE 0 END,
        avg_return_pct = sub.avg_ret,
        credibility_score = CASE WHEN sub.resolved >= 10 
                            THEN (sub.hits::NUMERIC / sub.resolved) * 0.6 + 
                                 LEAST(sub.avg_ret / 10.0, 0.4) 
                            ELSE 0.5 END,
        updated_at = NOW()
    FROM (
        SELECT 
            analyst_id,
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE status != 'active') as resolved,
            COUNT(*) FILTER (WHERE status = 'hit_target') as hits,
            COUNT(*) FILTER (WHERE status = 'hit_stoploss') as stops,
            COUNT(*) FILTER (WHERE status = 'expired') as expired,
            AVG(outcome_pnl_pct) FILTER (WHERE status != 'active') as avg_ret
        FROM signals
        WHERE analyst_id IS NOT NULL
        GROUP BY analyst_id
    ) sub
    WHERE a.id = sub.analyst_id;
END;
$$ LANGUAGE plpgsql;

-- Resolve expired signals (run every hour during market hours)
CREATE OR REPLACE FUNCTION resolve_expired_signals()
RETURNS INT AS $$
DECLARE
    resolved_count INT;
BEGIN
    UPDATE signals SET
        status = 'expired',
        outcome_date = NOW(),
        updated_at = NOW()
    WHERE status = 'active'
      AND expiry_date IS NOT NULL
      AND expiry_date < NOW();
    
    GET DIAGNOSTICS resolved_count = ROW_COUNT;
    RETURN resolved_count;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- Chain ingestion observability (migration 0005)
-- ============================================================

-- Per-instrument data tier (refreshed daily at 6 AM IST)
CREATE TABLE IF NOT EXISTS fno_collection_tiers (
    instrument_id     UUID PRIMARY KEY REFERENCES instruments(id),
    tier              INT NOT NULL CHECK (tier IN (1, 2)),
    avg_volume_5d     BIGINT,
    last_promoted_at  TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ DEFAULT NOW()
);

-- Per-poll outcome log (one row per underlying per attempted snapshot)
CREATE TABLE IF NOT EXISTS chain_collection_log (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    instrument_id     UUID NOT NULL REFERENCES instruments(id),
    attempted_at      TIMESTAMPTZ NOT NULL,
    primary_source    VARCHAR(20) NOT NULL,
    fallback_source   VARCHAR(20),
    final_source      VARCHAR(20),
    status            VARCHAR(20) NOT NULL CHECK (status IN ('ok','fallback_used','missed')),
    nse_error         TEXT,
    dhan_error        TEXT,
    latency_ms        INT
);
CREATE INDEX IF NOT EXISTS idx_chain_log_attempted
    ON chain_collection_log(attempted_at DESC);
CREATE INDEX IF NOT EXISTS idx_chain_log_status
    ON chain_collection_log(status);

-- Schema mismatches and sustained failures (drives GitHub issue creation)
CREATE TABLE IF NOT EXISTS chain_collection_issues (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source            VARCHAR(20) NOT NULL,
    instrument_id     UUID REFERENCES instruments(id),
    issue_type        VARCHAR(30) NOT NULL
                          CHECK (issue_type IN ('schema_mismatch','sustained_failure','auth_error')),
    error_message     TEXT NOT NULL,
    raw_response      TEXT,
    detected_at       TIMESTAMPTZ DEFAULT NOW(),
    github_issue_url  TEXT,
    resolved_at       TIMESTAMPTZ,
    resolved_by       VARCHAR(50)
);
CREATE INDEX IF NOT EXISTS idx_chain_issues_unresolved
    ON chain_collection_issues(detected_at DESC)
    WHERE resolved_at IS NULL;

-- Source health for the source-pluggable abstraction
CREATE TABLE IF NOT EXISTS source_health (
    source            VARCHAR(20) PRIMARY KEY,
    status            VARCHAR(20) NOT NULL
                          CHECK (status IN ('healthy','degraded','failed')),
    consecutive_errors INT DEFAULT 0,
    last_success_at   TIMESTAMPTZ,
    last_error_at     TIMESTAMPTZ,
    last_error        TEXT,
    updated_at        TIMESTAMPTZ DEFAULT NOW()
);

-- Seed source health rows
INSERT INTO source_health (source, status) VALUES
    ('nse',       'healthy'),
    ('dhan',      'healthy'),
    ('angel_one', 'healthy')
ON CONFLICT (source) DO NOTHING;

-- Add source provenance column to existing options_chain table
ALTER TABLE options_chain ADD COLUMN IF NOT EXISTS source VARCHAR(20) DEFAULT 'nse';
