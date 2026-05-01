"""
LEAN-inspired Alpha Framework for PaperBull.
Apache 2.0 pattern — implemented from scratch.

Mirrors QuantConnect LEAN's:
  UniverseSelection → AlphaModel → PortfolioConstruction → ExecutionModel
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Literal


@dataclass
class Insight:
    """A directional prediction from an alpha source. Mirrors LEAN's Insight."""

    ticker: str
    direction: Literal["UP", "DOWN", "FLAT"]
    confidence: float          # 0.0–1.0
    magnitude: float | None    # expected % move
    period_days: int           # holding period
    source: str                # which alpha model generated this
    generated_at: date = field(default_factory=date.today)


class UniverseModel(ABC):
    """Selects which instruments to analyse each day."""

    @abstractmethod
    async def select(self) -> list[str]:
        """Return list of NSE tickers in today's universe."""
        ...


class AlphaModel(ABC):
    """Generates Insights for tickers in the universe."""

    @abstractmethod
    async def generate(self, tickers: list[str]) -> list[Insight]:
        """Return Insight list for the provided tickers."""
        ...


class PortfolioConstructionModel(ABC):
    """Converts Insights into target paper positions."""

    @abstractmethod
    async def construct(self, insights: list[Insight]) -> list[dict]:
        """Return list of paper trade targets: {ticker, direction, size}"""
        ...


class ExecutionModel(ABC):
    """Executes paper trades via OpenAlgo sandbox."""

    @abstractmethod
    async def execute(self, targets: list[dict]) -> list[dict]:
        """Submit orders and return execution results."""
        ...


class PaperBullAlphaFramework:
    """
    Orchestrator. Run daily after market open or pre-market.
    """

    def __init__(
        self,
        universe: UniverseModel,
        alpha: AlphaModel,
        portfolio: PortfolioConstructionModel,
        execution: ExecutionModel,
    ) -> None:
        self.universe = universe
        self.alpha = alpha
        self.portfolio = portfolio
        self.execution = execution

    async def run_daily_cycle(self) -> dict:
        """Execute one full cycle: select → generate → construct → execute."""
        tickers = await self.universe.select()
        insights = await self.alpha.generate(tickers)
        targets = await self.portfolio.construct(insights)
        trades = await self.execution.execute(targets)
        return {"insights": len(insights), "trades": len(trades)}
