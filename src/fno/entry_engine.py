"""Bridge between Phase 3 PROCEED decisions and concrete option contract picks.

For each Phase 3 PROCEED candidate, this module:
  1. Pulls the latest options_chain snapshot for the underlying.
  2. Iterates every registered strategy and calls select() to get
     candidate Leg lists.
  3. Picks the highest-scoring StrategyRecommendation (ranker logic
     reuses simple priority order until the full strike_ranker.rank_strategies
     wiring is hardened).
  4. Returns a list of EntryProposal objects with the chosen strike(s),
     entry premium, target, and stop — ready to render in the morning brief
     and / or persist into fno_signals.

This module does NOT yet write to fno_signals — that's left to the caller
(orchestrator.morning_brief or Phase 4 entry tick) so the same selection
logic can be used both for the brief preview and for live entries.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from loguru import logger
from sqlalchemy import select

from src.db import session_scope
from src.fno.calendar import next_weekly_expiry
from src.fno.strategies.base import Leg, StrategyRecommendation
from src.fno.strategies.bear_put_spread import BearPutSpreadStrategy
from src.fno.strategies.bull_call_spread import BullCallSpreadStrategy
from src.fno.strategies.iron_condor import IronCondorStrategy
from src.fno.strategies.long_call import LongCallStrategy
from src.fno.strategies.long_put import LongPutStrategy
from src.fno.strategies.straddle import StraddleStrategy
from src.models.fno_candidate import FNOCandidate
from src.models.fno_chain import OptionsChain
from src.models.instrument import Instrument


_STRATEGIES = [
    LongCallStrategy(),
    LongPutStrategy(),
    BullCallSpreadStrategy(),
    BearPutSpreadStrategy(),
    StraddleStrategy(),
    IronCondorStrategy(),
]


@dataclass
class EntryProposal:
    """A concrete strategy + leg choice for a single Phase-3 PROCEED candidate."""
    instrument_id: str
    symbol: str
    direction: str
    expiry_date: date
    strategy_name: str
    legs: list[Leg]
    underlying_ltp: Decimal
    entry_premium: Decimal
    target_premium: Decimal | None
    stop_premium: Decimal | None
    max_risk: Decimal
    max_reward: Decimal
    notes: str
    candidate_id: str | None = None

    def legs_dict(self) -> list[dict]:
        return [
            {
                "option_type": leg.option_type,
                "strike": str(leg.strike),
                "action": leg.action,
                "quantity": leg.quantity,
            }
            for leg in self.legs
        ]

    def short_label(self) -> str:
        """Human-readable one-line contract summary for Telegram."""
        if not self.legs:
            return "(no legs)"
        if len(self.legs) == 1:
            leg = self.legs[0]
            return (
                f"{leg.action} {leg.option_type} {leg.strike} "
                f"@ ₹{self.entry_premium} (exp {self.expiry_date.strftime('%d-%b')})"
            )
        # multi-leg — show net premium and leg count
        parts = " / ".join(
            f"{leg.action[0]}{leg.option_type}{leg.strike}" for leg in self.legs
        )
        return (
            f"{self.strategy_name} [{parts}] net ₹{self.entry_premium} "
            f"(exp {self.expiry_date.strftime('%d-%b')})"
        )


def _direction_from_oi(oi_structure: str | None) -> str:
    if not oi_structure:
        return "neutral"
    s = oi_structure.lower()
    if "bull" in s or "long_call" in s or "long" in s:
        return "bullish"
    if "bear" in s or "long_put" in s or "short" in s:
        return "bearish"
    return "neutral"


def _direction_from_thesis(thesis: str | None) -> str:
    """Heuristic fallback when oi_structure is 'unknown' — scan the thesis text
    for direction words. PROCEED candidates almost always state direction
    explicitly somewhere in the thesis paragraph.
    """
    if not thesis:
        return "neutral"
    t = thesis.lower()
    bull_score = sum(t.count(w) for w in ("bullish", "buy", "long", "uptrend", "rally"))
    bear_score = sum(t.count(w) for w in ("bearish", "sell", "short", "downtrend", "decline"))
    if bull_score > bear_score:
        return "bullish"
    if bear_score > bull_score:
        return "bearish"
    return "neutral"


async def propose_entries(run_date: date | None = None) -> list[EntryProposal]:
    """Pick a concrete contract for every Phase 3 PROCEED candidate today.

    Returns one EntryProposal per candidate that has both:
      - A latest options_chain snapshot (for ATM premium + strike list)
      - At least one applicable strategy
    """
    if run_date is None:
        run_date = date.today()

    proposals: list[EntryProposal] = []

    async with session_scope() as session:
        rows = await session.execute(
            select(
                FNOCandidate.id,
                FNOCandidate.instrument_id,
                Instrument.symbol,
                FNOCandidate.iv_regime,
                FNOCandidate.oi_structure,
                FNOCandidate.llm_thesis,
            )
            .join(Instrument, Instrument.id == FNOCandidate.instrument_id)
            .where(
                FNOCandidate.run_date == run_date,
                FNOCandidate.phase == 3,
                FNOCandidate.llm_decision == "PROCEED",
                FNOCandidate.dryrun_run_id.is_(None),
            )
        )
        candidates = list(rows.all())

    for cand_id, inst_id, symbol, iv_regime, oi_structure, thesis in candidates:
        try:
            proposal = await _propose_one(
                cand_id=cand_id,
                instrument_id=inst_id,
                symbol=symbol,
                iv_regime=iv_regime or "neutral",
                oi_structure=oi_structure,
                thesis=thesis,
                run_date=run_date,
            )
            if proposal is not None:
                proposals.append(proposal)
        except Exception as exc:
            logger.warning(f"entry_engine: {symbol} skipped: {exc}")

    logger.info(
        f"entry_engine: produced {len(proposals)} proposals "
        f"out of {len(candidates)} PROCEED candidates"
    )
    return proposals


async def _propose_one(
    *,
    cand_id,
    instrument_id,
    symbol: str,
    iv_regime: str,
    oi_structure: str | None,
    thesis: str | None,
    run_date: date,
) -> EntryProposal | None:
    direction = _direction_from_oi(oi_structure)
    # If OI structure is unknown / balanced / neutral, fall back to parsing
    # the LLM thesis text for explicit bullish/bearish words. PROCEED
    # decisions almost always state direction in their thesis.
    if direction == "neutral":
        direction = _direction_from_thesis(thesis)

    expiry = next_weekly_expiry(symbol, reference=run_date)
    expiry_days = max(0, (expiry - run_date).days)

    async with session_scope() as session:
        # Latest snapshot for this instrument and expiry
        snap_row = await session.execute(
            select(
                OptionsChain.snapshot_at,
                OptionsChain.strike_price,
                OptionsChain.option_type,
                OptionsChain.ltp,
                OptionsChain.underlying_ltp,
            )
            .where(
                OptionsChain.instrument_id == instrument_id,
                OptionsChain.expiry_date == expiry,
            )
            .order_by(OptionsChain.snapshot_at.desc())
            .limit(500)
        )
        rows = snap_row.all()

    if not rows:
        # Fall back to whatever the latest snapshot is, regardless of expiry
        async with session_scope() as session:
            snap_row = await session.execute(
                select(
                    OptionsChain.snapshot_at,
                    OptionsChain.expiry_date,
                    OptionsChain.strike_price,
                    OptionsChain.option_type,
                    OptionsChain.ltp,
                    OptionsChain.underlying_ltp,
                )
                .where(OptionsChain.instrument_id == instrument_id)
                .order_by(OptionsChain.snapshot_at.desc())
                .limit(500)
            )
            rows = snap_row.all()
            if rows:
                expiry = rows[0].expiry_date
                expiry_days = max(0, (expiry - run_date).days)
                rows = [r for r in rows if r.expiry_date == expiry]

    if not rows:
        return None

    underlying_ltp = next(
        (Decimal(str(r.underlying_ltp)) for r in rows if r.underlying_ltp), None
    )
    if underlying_ltp is None or underlying_ltp <= 0:
        return None

    strikes = sorted({Decimal(str(r.strike_price)) for r in rows})
    atm_strike = min(strikes, key=lambda s: abs(s - underlying_ltp))
    atm_ce_ltp = next(
        (Decimal(str(r.ltp)) for r in rows
         if Decimal(str(r.strike_price)) == atm_strike
         and r.option_type == "CE" and r.ltp), None
    )
    atm_pe_ltp = next(
        (Decimal(str(r.ltp)) for r in rows
         if Decimal(str(r.strike_price)) == atm_strike
         and r.option_type == "PE" and r.ltp), None
    )
    atm_premium = atm_ce_ltp if direction == "bullish" else (atm_pe_ltp or atm_ce_ltp or Decimal("0"))
    if atm_premium == 0:
        atm_premium = atm_ce_ltp or atm_pe_ltp or Decimal("0")

    # iv_rank is not stored — use a midpoint mapped from iv_regime
    iv_rank = {"low": 25.0, "neutral": 50.0, "high": 75.0}.get(iv_regime, 50.0)

    best: tuple[StrategyRecommendation, str] | None = None
    for strat in _STRATEGIES:
        try:
            if not strat.is_applicable(direction, iv_regime, expiry_days):
                continue
            rec = strat.select(
                direction=direction,
                underlying_price=underlying_ltp,
                iv_rank=iv_rank,
                iv_regime=iv_regime,
                expiry_days=expiry_days,
                chain_strikes=strikes,
                atm_premium=atm_premium,
            )
        except Exception as exc:
            logger.debug(f"entry_engine: {symbol} {strat.name} select failed: {exc}")
            continue
        if rec is None or not rec.legs:
            continue
        # Prefer simpler strategies first (fewer legs); the strategy iteration
        # order above is roughly conviction-aligned (long_call first when bullish).
        if best is None or len(rec.legs) < len(best[0].legs):
            best = (rec, strat.name)

    if best is None:
        logger.debug(f"entry_engine: {symbol} no applicable strategy for direction={direction}")
        return None

    rec, strat_name = best
    entry_prem = atm_premium  # net debit for single-leg long premium
    target_prem = (entry_prem * Decimal("1.30")).quantize(Decimal("0.01"))
    stop_prem = (entry_prem * Decimal("0.70")).quantize(Decimal("0.01"))

    return EntryProposal(
        instrument_id=str(instrument_id),
        symbol=symbol,
        direction=direction,
        expiry_date=expiry,
        strategy_name=strat_name,
        legs=rec.legs,
        underlying_ltp=underlying_ltp,
        entry_premium=entry_prem,
        target_premium=target_prem,
        stop_premium=stop_prem,
        max_risk=rec.max_risk,
        max_reward=rec.max_reward,
        notes=rec.notes,
        candidate_id=str(cand_id) if cand_id else None,
    )
