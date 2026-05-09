"""BacktestRunner — orchestrates a date range through the orchestrator's loop.

The runner is a thin coordinator: it owns no business logic. For each
trading day it:

  1. Creates a ``backtest_runs`` row (or reuses an existing one for resume).
  2. Builds a backtest-mode ``OrchestratorContext`` — clock, feature store,
     universe selector, and trade recorder are all the backtest variants.
  3. Calls ``orchestrator.run_loop(portfolio_id, ctx=...)``.
  4. Reads the resulting ``backtest_runs`` row for the summary.

SOLID notes:
  * SRP — runner does *only* date-range orchestration. Per-day logic lives
    in the orchestrator; per-day persistence lives in the recorder.
  * DIP — depends on the orchestrator's public ``run_loop`` and on the
    abstractions in ``src.quant.context``. No coupling to concrete backtest
    classes beyond the construction site.
  * OCP — adding new metrics or progress hooks requires extending the
    summary dataclass, not modifying the runner's main loop.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Iterable

import pytz
from loguru import logger
from sqlalchemy import select

from src.config import get_settings
from src.db import session_scope
from src.models.backtest_run import BacktestRun
from src.models.backtest_trade import BacktestTrade
from src.models.portfolio import Portfolio
from src.quant import orchestrator
from src.quant.backtest.checks.lookahead import LookaheadGuard
from src.quant.backtest.clock import BacktestClock, trading_days_between
from src.quant.backtest.feature_store import BacktestFeatureStore
from src.quant.backtest.universe_top_gainers import TopGainersUniverseSelector
from src.quant.clock import BacktestClockAdapter
from src.quant.context import OrchestratorContext
from src.quant.recorder import BacktestTradeRecorder
from src.quant.universe import UniverseSelector


_IST = pytz.timezone("Asia/Kolkata")


def _maybe_tqdm(iterable, *, enabled: bool, desc: str):
    """Return a tqdm-wrapped iterable when available + enabled, else passthrough.

    tqdm is an optional dependency. When missing, the iterator is unchanged.
    Keeping it optional avoids bloating ``pyproject.toml``.

    Generator-safety: we read ``len(iterable)`` directly when ``__len__`` is
    available, never ``len(list(iterable))`` — that would exhaust generator
    inputs before tqdm gets to wrap them.
    """
    if not enabled:
        return iterable
    try:
        from tqdm import tqdm  # type: ignore[import-not-found]
    except ImportError:
        return iterable
    total = len(iterable) if hasattr(iterable, "__len__") else None
    return tqdm(iterable, desc=desc, total=total)


# ---------------------------------------------------------------------------
# Public result types
# ---------------------------------------------------------------------------

@dataclass
class SingleDayResult:
    """Outcome of one ``backtest_runs`` row."""

    backtest_date: date
    backtest_run_id: uuid.UUID
    starting_nav: float
    final_nav: float | None
    pnl_pct: float | None
    trade_count: int | None
    failed: bool = False
    error: str | None = None


@dataclass
class BacktestRangeResult:
    """Aggregate across all days in a range."""

    portfolio_id: uuid.UUID
    start_date: date
    end_date: date
    days: list[SingleDayResult] = field(default_factory=list)

    @property
    def n_days(self) -> int:
        return len(self.days)

    @property
    def n_failed(self) -> int:
        return sum(1 for d in self.days if d.failed)

    @property
    def total_trade_count(self) -> int:
        return sum(d.trade_count or 0 for d in self.days)

    @property
    def cumulative_pnl_pct(self) -> float:
        """Geometric chaining of per-day pnl_pct (compounded)."""
        nav = 1.0
        for d in self.days:
            if d.pnl_pct is not None:
                nav *= 1.0 + float(d.pnl_pct)
        return nav - 1.0


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class BacktestRunner:
    """Replays a date range through the orchestrator with backtest I/O.

    Args:
        portfolio_id: Portfolio whose primitives + bandit are being replayed.
        seed: Reproducibility seed — same seed → bit-identical trade IDs.
        holidays: NSE holiday set; skipped during enumeration.
        risk_free_rate: Override for BS pricing in the feature store.
            Defaults to lookup from rbi_repo_history (None).
        smile_method: IV smile method for chain synthesis. Defaults to
            ``laabh_quant_backtest_iv_smile_method`` from settings.
        chain_source / underlying_source: Provenance tags written into every
            trade row so reports can flag synthesized vs real fills.

    The runner is stateless across days — each day's orchestrator run gets a
    fresh context. Bandit posteriors persist across days via the existing
    ``persistence.save_eod`` / ``load_morning`` pair, matching live behavior.
    """

    def __init__(
        self,
        *,
        portfolio_id: uuid.UUID,
        seed: int = 42,
        holidays: Iterable[date] = (),
        risk_free_rate: float | None = None,
        smile_method: str | None = None,
        chain_source: str = "synthesized",
        underlying_source: str = "dhan_intraday",
        enable_lookahead_guard: bool = True,
    ) -> None:
        self._portfolio_id = portfolio_id
        self._seed = seed
        self._holidays = frozenset(holidays)
        self._risk_free_rate = risk_free_rate
        s = get_settings()
        self._smile_method = smile_method or s.laabh_quant_backtest_iv_smile_method
        self._chain_source = chain_source
        self._underlying_source = underlying_source
        # When True, every feature read is asserted against the virtual
        # clock. The guard is clock-aware so the orchestrator doesn't need
        # to call mark_now per tick. Disable only for benchmarking.
        self._enable_lookahead_guard = enable_lookahead_guard

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_range(
        self,
        start_date: date,
        end_date: date,
        *,
        as_of: datetime | None = None,
        dryrun_run_id: uuid.UUID | None = None,
        progress: bool = True,
    ) -> BacktestRangeResult:
        """Replay every trading day in ``[start, end]`` (inclusive).

        ``as_of`` and ``dryrun_run_id`` follow the CLAUDE.md convention.
        Backtest runs are inherently offline (they don't participate in the
        live tick-by-tick pipeline), so these parameters are accepted but
        not persisted to the ``backtest_runs`` / ``backtest_trades``
        tables (which lack the column entirely).

        ``progress`` controls a per-day progress bar. Defaults to True; set
        False in tests or when piping to a non-TTY. tqdm is optional —
        when missing, falls back to silent iteration.
        """
        days = trading_days_between(start_date, end_date, holidays=self._holidays)
        logger.info(
            f"BacktestRunner: replaying {len(days)} trading days for "
            f"portfolio {self._portfolio_id} (seed={self._seed})"
        )

        # Compounding: the running NAV starts at the live portfolio NAV on
        # day 1 and advances each day by the day's realized P&L. A failed
        # day leaves the running NAV unchanged (no fictitious P&L applied).
        running_nav = await self._fetch_nav(self._portfolio_id)

        result = BacktestRangeResult(
            portfolio_id=self._portfolio_id,
            start_date=start_date,
            end_date=end_date,
        )
        iterator = _maybe_tqdm(days, enabled=progress, desc="Backtest days")
        for d in iterator:
            single = await self._run_one_day(d, starting_nav=running_nav)
            result.days.append(single)
            if single.failed:
                logger.warning(
                    f"BacktestRunner: {d} failed — continuing. "
                    f"Error: {single.error}"
                )
            elif single.final_nav is not None:
                running_nav = single.final_nav
        logger.info(
            f"BacktestRunner: done. days={result.n_days} "
            f"failed={result.n_failed} cumulative_pnl_pct={result.cumulative_pnl_pct:.4%}"
        )
        return result

    # ------------------------------------------------------------------
    # Per-day execution
    # ------------------------------------------------------------------

    async def _run_one_day(
        self,
        trading_date: date,
        *,
        starting_nav: float | None = None,
    ) -> SingleDayResult:
        """Create a backtest_runs row, replay the day, return the summary.

        ``starting_nav`` is the NAV to compound from (carried over from the
        prior day's final NAV by ``run_range``). When None — e.g. when
        ``_run_one_day`` is invoked outside ``run_range`` — the live
        portfolio NAV is fetched as a fallback.
        """
        if starting_nav is None:
            starting_nav = await self._fetch_nav(self._portfolio_id)

        run_id = await self._create_backtest_run_row(
            trading_date=trading_date,
            starting_nav=starting_nav,
            universe=[],  # fill once selector runs
        )

        ctx = self._build_context(
            trading_date=trading_date,
            backtest_run_id=run_id,
            starting_nav=starting_nav,
        )

        # The orchestrator's hard-exit gate uses IST clock.time();
        # set our virtual ``as_of`` to session open so legacy gates compute
        # against the same instant the clock reports.
        as_of = _IST.localize(datetime.combine(trading_date, time(9, 15)))

        try:
            await orchestrator.run_loop(
                self._portfolio_id,
                as_of=as_of,
                ctx=ctx,
            )
        except Exception as exc:
            logger.exception(f"BacktestRunner: day {trading_date} threw")
            return SingleDayResult(
                backtest_date=trading_date,
                backtest_run_id=run_id,
                starting_nav=starting_nav,
                final_nav=None,
                pnl_pct=None,
                trade_count=None,
                failed=True,
                error=repr(exc),
            )

        return await self._read_run_summary(run_id, trading_date=trading_date)

    def _build_context(
        self,
        *,
        trading_date: date,
        backtest_run_id: uuid.UUID,
        starting_nav: float,
    ) -> OrchestratorContext:
        """Wire backtest-mode I/O into a fresh context for one day."""
        bt_clock = BacktestClock(
            trading_date=trading_date,
            holidays=self._holidays,
        )
        clock_adapter = BacktestClockAdapter(inner=bt_clock)
        bt_fs = BacktestFeatureStore(
            trading_date=trading_date,
            risk_free_rate=self._risk_free_rate,
            smile_method=self._smile_method,
        )
        # Optionally wrap the feature getter with the lookahead guard. The
        # guard reads the clock's ``now()`` automatically, so no per-tick
        # ``mark_now`` is needed in the orchestrator.
        feature_getter = bt_fs.get
        if self._enable_lookahead_guard:
            guard = LookaheadGuard(bt_fs.get, clock=clock_adapter)
            feature_getter = guard.checked_get
        # Universe selector is shared — takes its filters from settings.
        universe_selector: UniverseSelector = TopGainersUniverseSelector()
        recorder = BacktestTradeRecorder(
            backtest_run_id=backtest_run_id,
            chain_source=self._chain_source,
            underlying_source=self._underlying_source,
        )
        return OrchestratorContext(
            mode="backtest",
            clock=clock_adapter,
            feature_getter=feature_getter,
            universe_selector=universe_selector,
            recorder=recorder,
            nav_override=starting_nav,
        )

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _fetch_nav(portfolio_id: uuid.UUID) -> float:
        async with session_scope() as session:
            row = await session.get(Portfolio, portfolio_id)
            if row is None:
                return 0.0
            return float(row.current_cash or 0) + float(row.invested_value or 0)

    async def _create_backtest_run_row(
        self,
        *,
        trading_date: date,
        starting_nav: float,
        universe: list,
    ) -> uuid.UUID:
        s = get_settings()
        config_snapshot = self._snapshot_settings(s)
        async with session_scope() as session:
            row = BacktestRun(
                portfolio_id=self._portfolio_id,
                backtest_date=trading_date,
                config_snapshot=config_snapshot,
                universe=universe,
                starting_nav=Decimal(str(starting_nav)),
                bandit_seed=self._seed,
            )
            session.add(row)
            await session.flush()
            return row.id

    @staticmethod
    async def _read_run_summary(
        run_id: uuid.UUID, *, trading_date: date
    ) -> SingleDayResult:
        async with session_scope() as session:
            row = await session.get(BacktestRun, run_id)
            if row is None:
                # The fallback uses the requested trading_date (passed
                # through from the caller) rather than ``date.today()``,
                # so a row-not-found anomaly doesn't silently misattribute
                # the failure to the wrong calendar date in reports.
                return SingleDayResult(
                    backtest_date=trading_date,
                    backtest_run_id=run_id,
                    starting_nav=0.0,
                    final_nav=None,
                    pnl_pct=None,
                    trade_count=None,
                    failed=True,
                    error="backtest_run row not found post-replay",
                )
            return SingleDayResult(
                backtest_date=row.backtest_date,
                backtest_run_id=row.id,
                starting_nav=float(row.starting_nav),
                final_nav=float(row.final_nav) if row.final_nav is not None else None,
                pnl_pct=float(row.pnl_pct) if row.pnl_pct is not None else None,
                trade_count=row.trade_count,
            )

    def _snapshot_settings(self, s) -> dict:
        """Capture the QuantSettings that drove this run.

        Only the fields the orchestrator actually reads are snapshotted —
        dumping the entire Settings object would include secrets.
        """
        return {
            "primitives": s.quant_primitives_list,
            "bandit_algo": s.laabh_quant_bandit_algo,
            "forget_factor": s.laabh_quant_bandit_forget_factor,
            "kelly_fraction": s.laabh_quant_kelly_fraction,
            "max_per_trade_pct": s.laabh_quant_max_per_trade_pct,
            "max_total_exposure_pct": s.laabh_quant_max_total_exposure_pct,
            "lockin_target_pct": s.laabh_quant_lockin_target_pct,
            "kill_switch_pct": s.laabh_quant_kill_switch_dd_pct,
            "smile_method": self._smile_method,
            "seed": self._seed,
            "chain_source": self._chain_source,
            "underlying_source": self._underlying_source,
        }
