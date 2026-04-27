"""Base contract for every option chain data source."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import ClassVar


@dataclass
class StrikeRow:
    """Normalised per-strike row returned by any chain source."""

    strike: Decimal
    option_type: str  # "CE" or "PE"

    ltp: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None
    volume: int | None = None
    oi: int | None = None

    # Greeks — optional; populated by the parser when the source omits them
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None


@dataclass
class ChainSnapshot:
    """Normalised snapshot for one underlying at one point in time."""

    symbol: str
    expiry_date: date
    underlying_ltp: Decimal | None
    snapshot_at: datetime
    strikes: list[StrikeRow] = field(default_factory=list)

    def ce_strikes(self) -> list[StrikeRow]:
        return [s for s in self.strikes if s.option_type == "CE"]

    def pe_strikes(self) -> list[StrikeRow]:
        return [s for s in self.strikes if s.option_type == "PE"]


class BaseChainSource(ABC):
    """Contract every chain data source must satisfy."""

    name: ClassVar[str]

    @abstractmethod
    async def fetch(self, symbol: str, expiry_date: date) -> ChainSnapshot:
        """Return a normalised ChainSnapshot or raise a typed exception.

        Raises:
            SchemaError: response shape did not match expectations.
            RateLimitError: source rate-limited us.
            AuthError: credentials invalid or expired.
            SourceUnavailableError: any other failure (network, 5xx).
        """

    @abstractmethod
    async def health_check(self) -> bool:
        """Lightweight liveness probe used by the source_health updater."""
