"""Shared PostgreSQL enum definitions — `create_type=False` since schema.sql creates them."""
from __future__ import annotations

from sqlalchemy import Enum

SOURCE_TYPE = Enum(
    "rss_feed", "web_scraper", "api_feed", "youtube_live", "youtube_vod",
    "podcast", "twitter", "telegram_channel", "bse_filing", "nse_announcement",
    "broker_api", "manual",
    name="source_type", create_type=False,
)
SOURCE_STATUS = Enum(
    "active", "paused", "error", "disabled",
    name="source_status", create_type=False,
)
SIGNAL_ACTION = Enum(
    "BUY", "SELL", "HOLD", "WATCH",
    name="signal_action", create_type=False,
)
SIGNAL_TIMEFRAME = Enum(
    "intraday", "short_term", "medium_term", "long_term",
    name="signal_timeframe", create_type=False,
)
SIGNAL_STATUS = Enum(
    "active", "hit_target", "hit_stoploss", "expired", "cancelled",
    name="signal_status", create_type=False,
)
TRADE_TYPE = Enum("BUY", "SELL", name="trade_type", create_type=False)
TRADE_STATUS = Enum(
    "open", "closed", "cancelled", name="trade_status", create_type=False,
)
ORDER_TYPE = Enum(
    "MARKET", "LIMIT", "STOP_LOSS", "STOP_LOSS_MARKET",
    name="order_type", create_type=False,
)
NOTIFICATION_TYPE = Enum(
    "signal_alert", "price_alert", "watchlist_news", "trade_executed",
    "target_hit", "stoploss_hit", "analyst_call", "system",
    name="notification_type", create_type=False,
)
NOTIFICATION_PRIORITY = Enum(
    "low", "medium", "high", "critical",
    name="notification_priority", create_type=False,
)
MARKET_SEGMENT = Enum(
    "NSE_EQ", "BSE_EQ", "NSE_FO", "BSE_FO", "INDEX",
    name="market_segment", create_type=False,
)
