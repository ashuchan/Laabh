"""Universe selection for quant mode.

Live mode reads top-N candidates from ``fno_candidates`` (Phase 1-3 LLM
output). Backtest mode replaces this with the deterministic top-gainers
selector in ``src.quant.backtest.universe_top_gainers``. Both implement the
same ``UniverseSelector`` ABC so the orchestrator (Task 9 refactor) can
inject either one without further code changes.

The legacy module-level ``load_universe(date)`` is kept as a thin wrapper
around ``LLMUniverseSelector`` so existing call sites continue to work
without modification.
"""
from __future__ import annotations

import abc
from datetime import date

from loguru import logger
from sqlalchemy import desc, select  # noqa: F401 — desc used by HybridUniverseSelector

from src.config import get_settings
from src.db import session_scope
from src.models.fno_candidate import FNOCandidate
from src.models.instrument import Instrument


class UniverseSelector(abc.ABC):
    """Abstract universe selector — produces a list of underlyings for a date.

    Each implementation returns a list of dicts with keys ``id``, ``symbol``,
    and ``name`` (matches the existing live shape; see ``load_universe``).
    """

    @abc.abstractmethod
    async def select(self, trading_date: date) -> list[dict]:
        """Return ordered universe for ``trading_date``."""
        raise NotImplementedError


class LLMUniverseSelector(UniverseSelector):
    """Universe = top-N rows from ``fno_candidates`` ordered by composite_score.

    This is the live-mode selector. It assumes Phase 1-3 has already populated
    ``fno_candidates`` for ``trading_date``.
    """

    def __init__(self, *, size_cap: int | None = None) -> None:
        self._size_cap = size_cap

    async def select(self, trading_date: date) -> list[dict]:
        settings = get_settings()
        cap = self._size_cap or settings.laabh_quant_universe_size_cap

        async with session_scope() as session:
            # Drive-by fix: original used `FNOCandidate.date` which doesn't
            # exist on the model (the column is `run_date`). Every other
            # consumer in the repo correctly uses `run_date`.
            q = (
                select(FNOCandidate, Instrument)
                .join(Instrument, Instrument.id == FNOCandidate.instrument_id)
                .where(FNOCandidate.run_date == trading_date)
                .order_by(desc(FNOCandidate.composite_score))
                .limit(cap)
            )
            rows = (await session.execute(q)).all()

        result = [
            {"id": instr.id, "symbol": instr.symbol, "name": instr.name}
            for _candidate, instr in rows
        ]
        logger.info(
            f"LLMUniverseSelector: loaded {len(result)} underlyings for {trading_date}"
        )
        return result


class HybridUniverseSelector(UniverseSelector):
    """Universe = TopGainers base + Phase-3 PROCEED LLM supplements.

    Runs ``TopGainersUniverseSelector`` for a deterministic base set, then
    appends any instruments that received a Phase-3 LLM PROCEED decision today
    (up to ``llm_max_add`` extras) and are not already in the base set.

    The LLM supplement ensures stocks with genuine catalysts (earnings surprise,
    drug approval, M&A) enter the universe even when their D-1 momentum ranking
    was too low for the gainers/movers/gappers buckets alone.

    Used in live quant mode. Backtest mode should use ``TopGainersUniverseSelector``
    directly (LLM output is not historically reproducible).
    """

    def __init__(
        self,
        *,
        size_cap: int | None = None,
        llm_max_add: int | None = None,
    ) -> None:
        # Deferred import required: universe_top_gainers imports UniverseSelector
        # from this module, creating a circular dependency at module load time.
        # Deferring to __init__ breaks the cycle without moving UniverseSelector
        # to a separate file.
        from src.quant.backtest.universe_top_gainers import TopGainersUniverseSelector

        settings = get_settings()
        self._base = TopGainersUniverseSelector(
            size_cap=size_cap or settings.laabh_quant_universe_size_cap
        )
        self._llm_max_add = (
            llm_max_add
            if llm_max_add is not None
            else settings.laabh_quant_llm_supplement_max_add
        )
        self._llm_enabled = settings.laabh_quant_llm_supplement_enabled

    async def select(self, trading_date: date) -> list[dict]:
        base = await self._base.select(trading_date)
        if not self._llm_enabled:
            return base

        supplements = await self._load_llm_proceeds(trading_date)
        existing_symbols = {u["symbol"] for u in base}
        added = 0
        for instr in supplements:
            if added >= self._llm_max_add:
                break
            if instr["symbol"] in existing_symbols:
                continue
            base.append(instr)
            existing_symbols.add(instr["symbol"])
            added += 1

        if added:
            logger.info(
                f"HybridUniverseSelector: added {added} LLM-supplement instruments "
                f"for {trading_date} (total={len(base)})"
            )
        return base

    @staticmethod
    async def _load_llm_proceeds(
        trading_date: date,
        *,
        as_of=None,         # noqa: ARG004 — accepted for pipeline convention
        dryrun_run_id=None, # noqa: ARG004
    ) -> list[dict]:
        """Return instruments with Phase-3 PROCEED decision for trading_date."""
        from src.models.fno_candidate import FNOCandidate

        async with session_scope() as session:
            q = (
                select(FNOCandidate, Instrument)
                .join(Instrument, Instrument.id == FNOCandidate.instrument_id)
                .where(
                    FNOCandidate.run_date == trading_date,
                    FNOCandidate.phase == 3,
                    FNOCandidate.llm_decision == "PROCEED",
                )
                .order_by(desc(FNOCandidate.composite_score))
            )
            rows = (await session.execute(q)).all()

        result = [
            {
                "id": instr.id,
                "symbol": instr.symbol,
                "name": instr.company_name,
                "sector": instr.sector,   # needed by sector-heat bucket
            }
            for _cand, instr in rows
        ]
        if not result:
            logger.debug(
                f"HybridUniverseSelector: no Phase-3 PROCEED candidates for {trading_date} "
                f"— LLM supplement empty (Phase 3 may not have run yet)"
            )
        return result


async def load_universe(
    trading_date: date,
    *,
    as_of=None,
    dryrun_run_id=None,
) -> list[dict]:
    """Backwards-compatible wrapper used by the live orchestrator.

    Delegates to ``LLMUniverseSelector``. Live call sites that pre-date the
    Task 9 refactor (orchestrator → injected selector) keep working unchanged.
    """
    return await LLMUniverseSelector().select(trading_date)
