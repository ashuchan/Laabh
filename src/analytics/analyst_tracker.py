"""Match signals to analysts and compute credibility scores."""
from __future__ import annotations

import uuid
from decimal import Decimal

from loguru import logger
from sqlalchemy import func, select, text

from src.db import session_scope
from src.models.analyst import Analyst
from src.models.signal import Signal, SignalAutoTrade


class AnalystTracker:
    """Maintains analyst credibility scores based on resolved signal outcomes."""

    # Scoring weights as per spec
    HIT_RATE_WEIGHT = Decimal("0.40")
    RETURN_WEIGHT = Decimal("0.25")
    CONSISTENCY_WEIGHT = Decimal("0.20")
    RECENCY_WEIGHT = Decimal("0.15")

    RETURN_CAP = Decimal("10")  # cap avg return at 10% for normalization
    MIN_SIGNALS = 5
    DECAY_DAYS = 90  # signals > 90 days old contribute 50% weight

    async def update_all_scores(self) -> int:
        """Recalculate credibility scores for all analysts. Returns count updated."""
        async with session_scope() as session:
            result = await session.execute(select(Analyst))
            analysts = result.scalars().all()

        updated = 0
        for analyst in analysts:
            try:
                await self._update_analyst_score(analyst.id)
                updated += 1
            except Exception as exc:
                logger.error(f"analyst score update failed {analyst.id}: {exc}")

        logger.info(f"analyst scores updated: {updated}")
        return updated

    async def _update_analyst_score(self, analyst_id: uuid.UUID) -> None:
        """Compute and persist credibility_score for one analyst."""
        async with session_scope() as session:
            # Get all resolved auto-trades for this analyst
            result = await session.execute(
                select(SignalAutoTrade).where(
                    SignalAutoTrade.analyst_id == analyst_id,
                    SignalAutoTrade.status.in_(["hit_target", "hit_stoploss", "expired"]),
                )
            )
            trades = result.scalars().all()

        if len(trades) < self.MIN_SIGNALS:
            # Not enough data — leave at default 0.5
            return

        wins = [t for t in trades if t.status == "hit_target"]
        hit_rate = Decimal(str(len(wins))) / Decimal(str(len(trades)))

        returns = [Decimal(str(t.pnl_pct or 0)) for t in trades]
        avg_return = sum(returns) / len(returns)
        normalized_return = min(avg_return / self.RETURN_CAP, Decimal("1"))

        if len(returns) > 1:
            mean = avg_return
            variance = sum((r - mean) ** 2 for r in returns) / len(returns)
            std_dev = variance.sqrt()
            # Consistency = lower std_dev is better; scale to 0-1
            consistency = max(Decimal("0"), Decimal("1") - std_dev / Decimal("20"))
        else:
            consistency = Decimal("0.5")

        # Recency: count signals in last 90 days vs older
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=self.DECAY_DAYS)
        recent = [t for t in trades if t.created_at and t.created_at >= cutoff]
        recency_weight = (
            Decimal(str(len(recent))) / Decimal(str(len(trades)))
            if trades
            else Decimal("0.5")
        )

        score = (
            hit_rate * self.HIT_RATE_WEIGHT
            + normalized_return * self.RETURN_WEIGHT
            + consistency * self.CONSISTENCY_WEIGHT
            + recency_weight * self.RECENCY_WEIGHT
        )
        score = max(Decimal("0"), min(Decimal("1"), score))

        async with session_scope() as session:
            analyst = await session.get(Analyst, analyst_id)
            if analyst:
                analyst.credibility_score = float(score)
                analyst.total_signals = len(trades)
                # Update hit_rate on analyst model if the column exists
                if hasattr(analyst, "hit_rate"):
                    analyst.hit_rate = float(hit_rate)

        logger.debug(f"analyst {analyst_id} score={score:.3f} hit_rate={hit_rate:.2f}")
