"""Signal convergence engine — combine multiple sources for high-confidence alerts."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
from loguru import logger
from sqlalchemy import select

from src.db import session_scope
from src.models.analyst import Analyst
from src.models.price import PriceDaily
from src.models.signal import Signal
from src.models.source import DataSource
from src.services.notification_service import NotificationService


class ConvergenceEngine:
    """Calculates convergence scores across news, TV, and technical data."""

    HIGH_PRIORITY_THRESHOLD = 4
    CRITICAL_THRESHOLD = 5
    TRUSTED_ANALYST_CREDIBILITY = Decimal("0.6")
    WINDOW_HOURS = 24  # look back this many hours for related signals

    def __init__(self) -> None:
        self._notifier = NotificationService()

    async def run_convergence_check(self) -> int:
        """Recalculate convergence for all recently-active signals. Returns count updated."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.WINDOW_HOURS)
        async with session_scope() as session:
            result = await session.execute(
                select(Signal).where(
                    Signal.status == "active",
                    Signal.signal_date >= cutoff,
                )
            )
            signals = result.scalars().all()

        updated = 0
        # Group by instrument to avoid redundant checks
        instruments_seen: set[uuid.UUID] = set()
        for signal in signals:
            if signal.instrument_id not in instruments_seen:
                instruments_seen.add(signal.instrument_id)
                try:
                    await self._update_convergence(signal.instrument_id, cutoff)
                    updated += 1
                except Exception as exc:
                    logger.error(f"convergence check failed for {signal.instrument_id}: {exc}")

        logger.info(f"convergence: updated {updated} instruments")
        return updated

    async def on_new_signal(self, signal_id: uuid.UUID) -> None:
        """Called when a new signal is created — updates convergence immediately."""
        async with session_scope() as session:
            signal = await session.get(Signal, signal_id)
        if signal is None:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.WINDOW_HOURS)
        await self._update_convergence(signal.instrument_id, cutoff)

    async def _update_convergence(
        self, instrument_id: uuid.UUID, cutoff: datetime
    ) -> None:
        """Compute convergence score for all active signals on an instrument."""
        async with session_scope() as session:
            result = await session.execute(
                select(Signal).where(
                    Signal.instrument_id == instrument_id,
                    Signal.status == "active",
                    Signal.signal_date >= cutoff,
                )
            )
            signals = result.scalars().all()

        if len(signals) < 2:
            return

        # Separate by direction
        buys = [s for s in signals if s.action == "BUY"]
        sells = [s for s in signals if s.action == "SELL"]

        for group in (buys, sells):
            if len(group) < 2:
                continue
            score = await self._compute_score(group)
            related_ids = [s.id for s in group]

            async with session_scope() as session:
                for sig in group:
                    s = await session.get(Signal, sig.id)
                    if s:
                        s.convergence_score = score
                        s.related_signal_ids = related_ids

            if score >= self.HIGH_PRIORITY_THRESHOLD:
                await self._send_convergence_alert(group[0], score, group)

    async def _compute_score(self, signals: list[Signal]) -> int:
        score = 0
        seen_source_types: set[str] = set()

        for sig in signals:
            source_type = await self._get_source_type(sig.source_id)
            if source_type and source_type not in seen_source_types:
                score += 2  # cross-source bonus
                seen_source_types.add(source_type)
            else:
                score += 1  # same-source confirmation

            if sig.analyst_id:
                credibility = await self._get_analyst_credibility(sig.analyst_id)
                if credibility and credibility > self.TRUSTED_ANALYST_CREDIBILITY:
                    score += 1

        return score

    async def _send_convergence_alert(
        self, lead_signal: Signal, score: int, related: list[Signal]
    ) -> None:
        priority = "critical" if score >= self.CRITICAL_THRESHOLD else "high"
        body = (
            f"Convergence score {score}: {len(related)} sources agree on "
            f"{lead_signal.action}"
        )
        if lead_signal.target_price:
            body += f" → target ₹{lead_signal.target_price}"
        if lead_signal.stop_loss:
            body += f" | SL ₹{lead_signal.stop_loss}"
        await self._notifier.create(
            type_="signal_alert",
            title=f"High Convergence: {lead_signal.action} (score {score})",
            body=body,
            priority=priority,
            instrument_id=lead_signal.instrument_id,
            signal_id=lead_signal.id,
        )

    async def validate_signal_with_technicals(
        self, signal: Signal, lookback_days: int = 60
    ) -> tuple[bool, list[str], list[str]]:
        """Cross-check a signal with RSI, MACD, SMA. Returns (confirmed, conflicts, confirmations)."""
        prices = await self._load_prices(signal.instrument_id, lookback_days)
        if len(prices) < 26:
            return True, [], []  # not enough data — don't block

        closes = np.array(prices, dtype=float)
        rsi = self._calc_rsi(closes)
        macd_line, signal_line = self._calc_macd(closes)
        sma_20 = float(np.mean(closes[-20:]))
        sma_50 = float(np.mean(closes[-50:])) if len(closes) >= 50 else None
        current = float(closes[-1])

        conflicts: list[str] = []
        confirmations: list[str] = []

        if signal.action == "BUY":
            if rsi > 70:
                conflicts.append(f"RSI overbought ({rsi:.0f})")
            if rsi < 30:
                confirmations.append(f"RSI oversold ({rsi:.0f}) — good entry")
            if current > sma_20 and (sma_50 is None or sma_20 > sma_50):
                confirmations.append("Price above 20 & 50 SMA")
            if macd_line > signal_line:
                confirmations.append("MACD bullish crossover")
        elif signal.action == "SELL":
            if rsi < 30:
                conflicts.append(f"RSI oversold ({rsi:.0f})")
            if rsi > 70:
                confirmations.append(f"RSI overbought ({rsi:.0f}) — supports sell")
            if current < sma_20:
                confirmations.append("Price below 20 SMA")
            if macd_line < signal_line:
                confirmations.append("MACD bearish crossover")

        is_confirmed = len(confirmations) >= len(conflicts)
        return is_confirmed, conflicts, confirmations

    def _calc_rsi(self, closes: np.ndarray, period: int = 14) -> float:
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0)[-period:]
        losses = np.where(deltas < 0, -deltas, 0)[-period:]
        avg_gain = np.mean(gains) if gains.size else 0
        avg_loss = np.mean(losses) if losses.size else 1e-9
        rs = avg_gain / avg_loss if avg_loss else float("inf")
        return 100 - (100 / (1 + rs))

    def _calc_macd(
        self, closes: np.ndarray, fast: int = 12, slow: int = 26, signal_period: int = 9
    ) -> tuple[float, float]:
        ema_fast = self._ema(closes, fast)
        ema_slow = self._ema(closes, slow)
        macd = ema_fast - ema_slow
        signal_line = self._ema(macd, signal_period) if len(macd) >= signal_period else macd
        return float(macd[-1]), float(signal_line[-1])

    def _ema(self, data: np.ndarray, period: int) -> np.ndarray:
        k = 2 / (period + 1)
        ema = np.zeros_like(data, dtype=float)
        ema[0] = data[0]
        for i in range(1, len(data)):
            ema[i] = data[i] * k + ema[i - 1] * (1 - k)
        return ema

    async def _load_prices(self, instrument_id: uuid.UUID, days: int) -> list[float]:
        async with session_scope() as session:
            result = await session.execute(
                select(PriceDaily.close)
                .where(PriceDaily.instrument_id == instrument_id)
                .order_by(PriceDaily.date.desc())
                .limit(days)
            )
            rows = result.scalars().all()
        return list(reversed([float(r) for r in rows]))

    async def _get_source_type(self, source_id: uuid.UUID) -> str | None:
        async with session_scope() as session:
            source = await session.get(DataSource, source_id)
            return source.type if source else None

    async def _get_analyst_credibility(self, analyst_id: uuid.UUID) -> Decimal | None:
        async with session_scope() as session:
            analyst = await session.get(Analyst, analyst_id)
            return Decimal(str(analyst.credibility_score)) if analyst else None
