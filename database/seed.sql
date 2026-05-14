-- ============================================================================
-- LAABH — Seed Data
-- Run after schema.sql to populate initial sources, instruments, watchlists
-- ============================================================================

-- ============================================================================
-- DATA SOURCES — Pre-configured sources
-- ============================================================================

-- RSS Feeds
INSERT INTO data_sources (name, type, config, poll_interval_sec, priority, extraction_schema) VALUES
('Moneycontrol Markets', 'rss_feed', 
 '{"url": "https://www.moneycontrol.com/rss/marketreports.xml", "category": "market_news"}',
 300, 2,
 '{"extract_signals": true, "extract_sentiment": true, "language_hint": "en"}'),

('Moneycontrol Business', 'rss_feed',
 '{"url": "https://www.moneycontrol.com/rss/business.xml", "category": "business_news"}',
 600, 4,
 '{"extract_signals": true, "extract_sentiment": true, "language_hint": "en"}'),

('ET Markets', 'rss_feed',
 '{"url": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms", "category": "market_news"}',
 300, 2,
 '{"extract_signals": true, "extract_sentiment": true, "language_hint": "en"}'),

('LiveMint Markets', 'rss_feed',
 '{"url": "https://www.livemint.com/rss/markets", "category": "market_news"}',
 300, 3,
 '{"extract_signals": true, "extract_sentiment": true, "language_hint": "en"}'),

('Business Standard Markets', 'rss_feed',
 '{"url": "https://www.business-standard.com/rss/markets-106.rss", "category": "market_news"}',
 600, 4,
 '{"extract_signals": true, "extract_sentiment": true, "language_hint": "en"}'),

('NDTV Profit', 'rss_feed',
 '{"url": "https://www.ndtvprofit.com/rss", "category": "market_news"}',
 600, 4,
 '{"extract_signals": true, "extract_sentiment": true, "language_hint": "en"}'),

('Reuters India Business', 'rss_feed',
 '{"url": "https://news.google.com/rss/search?q=reuters+india+business+economy&hl=en-IN&gl=IN&ceid=IN:en", "category": "macro_news"}',
 600, 5,
 '{"extract_signals": false, "extract_sentiment": true, "language_hint": "en"}');

-- Exchange Filings
INSERT INTO data_sources (name, type, config, poll_interval_sec, priority, extraction_schema) VALUES
('BSE Corporate Announcements', 'api_feed',
 '{"base_url": "https://api.bseindia.com/BseIndiaAPI/api", "endpoint": "/AnnSubCategoryGetData/GetBSEAnnData", "category": "filings"}',
 180, 1,
 '{"extract_signals": true, "extract_sentiment": true, "focus": "earnings,dividends,board_meetings"}'),

('NSE Corporate Actions', 'web_scraper',
 '{"url": "https://www.nseindia.com/companies-listing/corporate-filings-actions", "category": "filings"}',
 300, 1,
 '{"extract_signals": true, "focus": "bonus,splits,dividends,rights"}');

-- YouTube Channels (for Whisper pipeline)
INSERT INTO data_sources (name, type, config, poll_interval_sec, priority, extraction_schema) VALUES
('CNBC-TV18 Live', 'youtube_live',
 '{"channel_id": "UCmTFAGBjQPsUb-JKS2tKSdA", "channel_name": "CNBC-TV18", "stream_mode": "live", "language": "en-hi"}',
 60, 2,
 '{"extract_signals": true, "extract_analyst_name": true, "extract_price_targets": true}'),

('Zee Business Live', 'youtube_live',
 '{"channel_id": "UCkBSgMqvMWMHHt0bCNA-pjQ", "channel_name": "Zee Business", "stream_mode": "live", "language": "hi"}',
 60, 2,
 '{"extract_signals": true, "extract_analyst_name": true, "extract_price_targets": true}'),

('NDTV Profit Live', 'youtube_live',
 '{"channel_id": "UCkknGAmRpVN8Y_YnTOJpMuA", "channel_name": "NDTV Profit", "stream_mode": "live", "language": "en"}',
 60, 3,
 '{"extract_signals": true, "extract_analyst_name": true, "extract_price_targets": true}'),

('ET Now Live', 'youtube_live',
 '{"channel_id": "UCX20QHcsfy-UxMI2y_JvSBg", "channel_name": "ET Now", "stream_mode": "live", "language": "en"}',
 60, 3,
 '{"extract_signals": true, "extract_analyst_name": true, "extract_price_targets": true}');

-- Broker API
INSERT INTO data_sources (name, type, config, poll_interval_sec, priority, extraction_schema) VALUES
('Angel One SmartAPI', 'broker_api',
 '{"provider": "angel_one", "api_key": "", "client_id": "", "password": "", "totp_secret": "", "feed_type": "websocket"}',
 0, 1,
 '{}');

-- Google News
INSERT INTO data_sources (name, type, config, poll_interval_sec, priority, extraction_schema) VALUES
('Google News - Indian Stocks', 'web_scraper',
 '{"url": "https://news.google.com/rss/search?q=indian+stock+market+NSE+BSE&hl=en-IN&gl=IN&ceid=IN:en", "category": "aggregated_news"}',
 600, 5,
 '{"extract_signals": true, "extract_sentiment": true, "language_hint": "en"}');

-- ============================================================================
-- NIFTY 50 INSTRUMENTS (core watchlist)
-- ============================================================================

INSERT INTO instruments (symbol, exchange, company_name, sector, industry, yahoo_symbol, is_fno) VALUES
('RELIANCE', 'NSE', 'Reliance Industries Ltd', 'Energy', 'Oil & Gas Refining', 'RELIANCE.NS', true),
('TCS', 'NSE', 'Tata Consultancy Services Ltd', 'IT', 'IT Services', 'TCS.NS', true),
('HDFCBANK', 'NSE', 'HDFC Bank Ltd', 'Financials', 'Private Banks', 'HDFCBANK.NS', true),
('INFY', 'NSE', 'Infosys Ltd', 'IT', 'IT Services', 'INFY.NS', true),
('ICICIBANK', 'NSE', 'ICICI Bank Ltd', 'Financials', 'Private Banks', 'ICICIBANK.NS', true),
('HINDUNILVR', 'NSE', 'Hindustan Unilever Ltd', 'FMCG', 'Personal Care', 'HINDUNILVR.NS', true),
('ITC', 'NSE', 'ITC Ltd', 'FMCG', 'Tobacco & Cigarettes', 'ITC.NS', true),
('SBIN', 'NSE', 'State Bank of India', 'Financials', 'Public Banks', 'SBIN.NS', true),
('BHARTIARTL', 'NSE', 'Bharti Airtel Ltd', 'Telecom', 'Telecom Services', 'BHARTIARTL.NS', true),
('KOTAKBANK', 'NSE', 'Kotak Mahindra Bank Ltd', 'Financials', 'Private Banks', 'KOTAKBANK.NS', true),
('LT', 'NSE', 'Larsen & Toubro Ltd', 'Industrials', 'Construction & Engineering', 'LT.NS', true),
('AXISBANK', 'NSE', 'Axis Bank Ltd', 'Financials', 'Private Banks', 'AXISBANK.NS', true),
('ASIANPAINT', 'NSE', 'Asian Paints Ltd', 'Materials', 'Paints', 'ASIANPAINT.NS', true),
('MARUTI', 'NSE', 'Maruti Suzuki India Ltd', 'Auto', 'Passenger Vehicles', 'MARUTI.NS', true),
('SUNPHARMA', 'NSE', 'Sun Pharmaceutical Industries', 'Pharma', 'Pharmaceuticals', 'SUNPHARMA.NS', true),
('TATAMOTORS', 'NSE', 'Tata Motors Ltd', 'Auto', 'Commercial Vehicles', 'TATAMOTORS.NS', true),
('TITAN', 'NSE', 'Titan Company Ltd', 'Consumer Discretionary', 'Jewellery', 'TITAN.NS', true),
('BAJFINANCE', 'NSE', 'Bajaj Finance Ltd', 'Financials', 'NBFC', 'BAJFINANCE.NS', true),
('WIPRO', 'NSE', 'Wipro Ltd', 'IT', 'IT Services', 'WIPRO.NS', true),
('HCLTECH', 'NSE', 'HCL Technologies Ltd', 'IT', 'IT Services', 'HCLTECH.NS', true),
('ADANIENT', 'NSE', 'Adani Enterprises Ltd', 'Conglomerate', 'Diversified', 'ADANIENT.NS', true),
('ADANIPORTS', 'NSE', 'Adani Ports & SEZ Ltd', 'Industrials', 'Ports & Logistics', 'ADANIPORTS.NS', true),
('POWERGRID', 'NSE', 'Power Grid Corporation', 'Utilities', 'Power Transmission', 'POWERGRID.NS', true),
('NTPC', 'NSE', 'NTPC Ltd', 'Utilities', 'Power Generation', 'NTPC.NS', true),
('TATASTEEL', 'NSE', 'Tata Steel Ltd', 'Materials', 'Steel', 'TATASTEEL.NS', true),
('ULTRACEMCO', 'NSE', 'UltraTech Cement Ltd', 'Materials', 'Cement', 'ULTRACEMCO.NS', true),
('TECHM', 'NSE', 'Tech Mahindra Ltd', 'IT', 'IT Services', 'TECHM.NS', true),
('NESTLEIND', 'NSE', 'Nestle India Ltd', 'FMCG', 'Food Products', 'NESTLEIND.NS', true),
('BAJAJFINSV', 'NSE', 'Bajaj Finserv Ltd', 'Financials', 'Financial Services', 'BAJAJFINSV.NS', true),
('JSWSTEEL', 'NSE', 'JSW Steel Ltd', 'Materials', 'Steel', 'JSWSTEEL.NS', true),
('ONGC', 'NSE', 'Oil & Natural Gas Corp', 'Energy', 'Oil & Gas Exploration', 'ONGC.NS', true),
('M&M', 'NSE', 'Mahindra & Mahindra Ltd', 'Auto', 'Passenger Vehicles', 'M&M.NS', true),
('COALINDIA', 'NSE', 'Coal India Ltd', 'Energy', 'Mining', 'COALINDIA.NS', true),
('DRREDDY', 'NSE', 'Dr. Reddys Laboratories', 'Pharma', 'Pharmaceuticals', 'DRREDDY.NS', true),
('CIPLA', 'NSE', 'Cipla Ltd', 'Pharma', 'Pharmaceuticals', 'CIPLA.NS', true),
('EICHERMOT', 'NSE', 'Eicher Motors Ltd', 'Auto', 'Two Wheelers', 'EICHERMOT.NS', true),
('DIVISLAB', 'NSE', 'Divis Laboratories', 'Pharma', 'Pharmaceuticals', 'DIVISLAB.NS', true),
('BPCL', 'NSE', 'Bharat Petroleum Corp', 'Energy', 'Oil & Gas Refining', 'BPCL.NS', true),
('BRITANNIA', 'NSE', 'Britannia Industries', 'FMCG', 'Food Products', 'BRITANNIA.NS', true),
('HEROMOTOCO', 'NSE', 'Hero MotoCorp Ltd', 'Auto', 'Two Wheelers', 'HEROMOTOCO.NS', true),
('INDUSINDBK', 'NSE', 'IndusInd Bank Ltd', 'Financials', 'Private Banks', 'INDUSINDBK.NS', true),
('APOLLOHOSP', 'NSE', 'Apollo Hospitals', 'Healthcare', 'Hospitals', 'APOLLOHOSP.NS', true),
('GRASIM', 'NSE', 'Grasim Industries', 'Materials', 'Cement & Diversified', 'GRASIM.NS', true),
('TATACONSUM', 'NSE', 'Tata Consumer Products', 'FMCG', 'Food & Beverages', 'TATACONSUM.NS', true),
('BAJAJ-AUTO', 'NSE', 'Bajaj Auto Ltd', 'Auto', 'Two & Three Wheelers', 'BAJAJ-AUTO.NS', true),
('SBILIFE', 'NSE', 'SBI Life Insurance', 'Financials', 'Insurance', 'SBILIFE.NS', true),
('HDFCLIFE', 'NSE', 'HDFC Life Insurance', 'Financials', 'Insurance', 'HDFCLIFE.NS', true),
('WIPRO', 'NSE', 'Wipro Ltd', 'IT', 'IT Services', 'WIPRO.NS', true),
('SHRIRAMFIN', 'NSE', 'Shriram Finance Ltd', 'Financials', 'NBFC', 'SHRIRAMFIN.NS', true),
('TRENT', 'NSE', 'Trent Ltd', 'Consumer Discretionary', 'Retail', 'TRENT.NS', true)
ON CONFLICT (symbol, exchange) DO NOTHING;

-- Indices
INSERT INTO instruments (symbol, exchange, company_name, sector, yahoo_symbol, is_index) VALUES
('NIFTY 50', 'NSE', 'Nifty 50 Index', 'Index', '^NSEI', true),
('SENSEX', 'BSE', 'S&P BSE Sensex', 'Index', '^BSESN', true),
('BANKNIFTY', 'NSE', 'Nifty Bank Index', 'Index', '^NSEBANK', true),
('NIFTYIT', 'NSE', 'Nifty IT Index', 'Index', '^CNXIT', true)
ON CONFLICT (symbol, exchange) DO NOTHING;

-- ============================================================================
-- DEFAULT WATCHLIST
-- ============================================================================

INSERT INTO watchlists (name, description, is_default) VALUES
('Core Watchlist', 'Primary stocks to track daily', true),
('Swing Trades', 'Short-term momentum plays', false),
('Long Term', 'Blue chips for long-term holding', false);

-- Add top stocks to default watchlist
INSERT INTO watchlist_items (watchlist_id, instrument_id, alert_on_news, alert_on_signals)
SELECT 
    (SELECT id FROM watchlists WHERE is_default = true),
    id,
    true,
    true
FROM instruments 
WHERE symbol IN ('RELIANCE', 'TCS', 'HDFCBANK', 'INFY', 'ICICIBANK', 
                  'SBIN', 'BHARTIARTL', 'ITC', 'TATAMOTORS', 'BAJFINANCE')
AND exchange = 'NSE';

-- ============================================================================
-- DEFAULT PORTFOLIO
-- ============================================================================

INSERT INTO portfolios (name, initial_capital, current_cash, benchmark_symbol) VALUES
('Main Portfolio', 1000000.00, 1000000.00, 'NIFTY 50');
