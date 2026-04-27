"""SQLAlchemy ORM models — mirror the schema defined in database/schema.sql."""
from src.models.analyst import Analyst
from src.models.content import RawContent
from src.models.fno_ban import FNOBanList
from src.models.fno_candidate import FNOCandidate
from src.models.fno_chain import OptionsChain
from src.models.fno_chain_issue import ChainCollectionIssue
from src.models.fno_chain_log import ChainCollectionLog
from src.models.fno_collection_tier import FNOCollectionTier
from src.models.fno_cooldown import FNOCooldown
from src.models.fno_iv import IVHistory
from src.models.fno_ranker_config import RankerConfig
from src.models.fno_signal import FNOSignal, FNOSignalEvent
from src.models.fno_source_health import SourceHealth
from src.models.fno_vix import VIXTick
from src.models.instrument import Instrument
from src.models.llm_audit_log import LLMAuditLog
from src.models.notification import Notification
from src.models.pending_order import PendingOrder
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
    "ChainCollectionIssue",
    "ChainCollectionLog",
    "DataSource",
    "FNOBanList",
    "FNOCandidate",
    "FNOCollectionTier",
    "FNOCooldown",
    "FNOSignal",
    "FNOSignalEvent",
    "Holding",
    "IVHistory",
    "Instrument",
    "JobLog",
    "LLMAuditLog",
    "Notification",
    "OptionsChain",
    "PendingOrder",
    "Portfolio",
    "PortfolioSnapshot",
    "PriceDaily",
    "PriceTick",
    "RankerConfig",
    "RawContent",
    "Signal",
    "SignalAutoTrade",
    "SourceHealth",
    "SystemConfig",
    "Trade",
    "VIXTick",
    "Watchlist",
    "WatchlistItem",
    "register_all_models",
]
