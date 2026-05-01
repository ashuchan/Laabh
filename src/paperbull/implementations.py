"""
Concrete implementations of the Alpha Framework components.
Wires together: Nifty500Universe, MultiAgentAlpha, EqualWeightPortfolio, OpenAlgoExecution.
"""
from __future__ import annotations

import asyncio
from datetime import date

from src.paperbull.alpha_framework import (
    AlphaModel,
    ExecutionModel,
    Insight,
    PortfolioConstructionModel,
    UniverseModel,
)


class Nifty500Universe(UniverseModel):
    """Select from Nifty 500 filtered by signals in last 24h."""

    async def select(self) -> list[str]:
        """Return tickers that have received signals in the past 24 hours."""
        from src.db import session_scope
        from sqlalchemy import text

        async with session_scope() as session:
            rows = await session.execute(
                text("""
                    SELECT DISTINCT i.symbol
                    FROM signals s
                    JOIN instruments i ON i.id = s.instrument_id
                    WHERE s.created_at >= NOW() - INTERVAL '24 hours'
                      AND i.symbol IS NOT NULL
                    LIMIT 50
                """)
            )
            return [r[0] for r in rows.fetchall()]


class MultiAgentAlpha(AlphaModel):
    """Uses TradingAgents debate as the alpha source."""

    async def generate(self, tickers: list[str]) -> list[Insight]:
        """Run debate for each ticker (capped at 10 to manage LLM cost)."""
        from src.integrations.tradingagents.debate import debate_signal

        direction_map = {
            "Buy": "UP",
            "Overweight": "UP",
            "Sell": "DOWN",
            "Underweight": "DOWN",
            "Hold": "FLAT",
        }
        insights: list[Insight] = []
        for ticker in tickers[:10]:
            result = await asyncio.to_thread(debate_signal, ticker, date.today().isoformat())
            insights.append(
                Insight(
                    ticker=ticker,
                    direction=direction_map.get(result["decision"], "FLAT"),
                    confidence=result["confidence"],
                    magnitude=None,
                    period_days=5,
                    source="multi_agent_debate",
                )
            )
        return insights


class EqualWeightPortfolio(PortfolioConstructionModel):
    """Simple equal-weight, long-only, max 5 positions."""

    async def construct(self, insights: list[Insight]) -> list[dict]:
        """Filter actionable insights and return top-5 by confidence."""
        actionable = [
            i for i in insights if i.direction != "FLAT" and i.confidence >= 0.65
        ]
        actionable = sorted(actionable, key=lambda x: x.confidence, reverse=True)[:5]
        return [
            {"ticker": i.ticker, "direction": i.direction, "size": 1}
            for i in actionable
        ]


class OpenAlgoExecution(ExecutionModel):
    """Routes paper trades through OpenAlgo sandbox."""

    async def execute(self, targets: list[dict]) -> list[dict]:
        """Submit each target as a paper order via OpenAlgo."""
        from src.integrations.openalgo.client import place_paper_order

        results = []
        for t in targets:
            action = "BUY" if t["direction"] == "UP" else "SELL"
            result = await asyncio.to_thread(place_paper_order, t["ticker"], "NSE", action, t["size"])
            results.append(result)
        return results
