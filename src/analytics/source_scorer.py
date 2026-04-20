"""Score data sources by the quality of signals they produce."""
from __future__ import annotations

import uuid
from decimal import Decimal

from loguru import logger
from sqlalchemy import func, select

from src.db import session_scope
from src.models.signal import SignalAutoTrade
from src.models.source import DataSource


class SourceScorer:
    """Aggregates auto-trade outcomes by data source to rank signal quality."""

    MIN_SIGNALS = 5

    async def update_all_source_scores(self) -> None:
        """Recalculate quality metrics for all active sources."""
        async with session_scope() as session:
            result = await session.execute(select(DataSource))
            sources = result.scalars().all()

        for source in sources:
            try:
                await self._score_source(source.id)
            except Exception as exc:
                logger.error(f"source scoring failed for {source.id}: {exc}")

    async def _score_source(self, source_id: uuid.UUID) -> None:
        async with session_scope() as session:
            result = await session.execute(
                select(SignalAutoTrade).where(
                    SignalAutoTrade.source_id == source_id,
                    SignalAutoTrade.status.in_(["hit_target", "hit_stoploss", "expired"]),
                )
            )
            trades = result.scalars().all()

        if len(trades) < self.MIN_SIGNALS:
            return

        total = len(trades)
        wins = sum(1 for t in trades if t.status == "hit_target")
        hit_rate = wins / total
        avg_return = (
            sum(float(t.pnl_pct or 0) for t in trades) / total
        )

        async with session_scope() as session:
            source = await session.get(DataSource, source_id)
            if source and hasattr(source, "signal_hit_rate"):
                source.signal_hit_rate = hit_rate
                source.signal_avg_return = avg_return
                source.signals_resolved = total

        logger.debug(f"source {source_id} hit_rate={hit_rate:.2f} avg_return={avg_return:.2f}%")
