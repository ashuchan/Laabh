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
from sqlalchemy import desc, select

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
