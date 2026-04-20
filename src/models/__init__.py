"""SQLAlchemy ORM models — mirror the schema defined in database/schema.sql."""
from src.models.analyst import Analyst
from src.models.content import RawContent
from src.models.instrument import Instrument
from src.models.notification import Notification
from src.models.portfolio import Holding, Portfolio, PortfolioSnapshot
from src.models.price import PriceDaily, PriceTick
from src.models.signal import Signal, SignalAutoTrade
from src.models.source import DataSource, JobLog, SystemConfig
from src.models.trade import Trade
from src.models.watchlist import Watchlist, WatchlistItem


def register_all_models() -> None:
    """No-op — importing this module registers every model on Base.metadata."""


__all__ = [
    "Analyst",
    "DataSource",
    "Holding",
    "Instrument",
    "JobLog",
    "Notification",
    "Portfolio",
    "PortfolioSnapshot",
    "PriceDaily",
    "PriceTick",
    "RawContent",
    "Signal",
    "SignalAutoTrade",
    "SystemConfig",
    "Trade",
    "Watchlist",
    "WatchlistItem",
    "register_all_models",
]
