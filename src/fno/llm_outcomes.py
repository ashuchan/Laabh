"""LLM outcome attribution — Phase 0.3.

Plan reference: docs/llm_feature_generator/implementation_plan.md §0.3.

Runs on a polling cadence (5 min default; see scheduler). On each tick:

  1. Find ``llm_decision_log`` rows where ``outcome_attributed_at IS NULL``.
  2. For each, locate the downstream ``fno_signal`` via the
     ``fno_candidates.id`` lineage and inspect its status:

       * ``status='closed'``  → write ``outcome_class='traded'`` with
         ``outcome_pnl_pct`` from realised P&L and ``outcome_z`` from the
         daily realised-vol scaling.
       * No signal at all and run_date is ≥5 trading days old → counterfactual.
         v10 rows carry ``proposed_strikes`` so a synthetic P&L can be priced
         from ``options_chain`` snapshots; v9 rows don't, so they degrade to
         ``unobservable``.
       * Open ≥30 trading days → ``timeout``, excluded from calibration.

  3. Persist ``outcome_attributed_at`` so the row is not visited again.

The job is idempotent (its select filter excludes already-attributed rows) and
safe to call concurrently — the unique constraint on ``llm_decision_log``
plus a row-level update protect against double-writes.

Follows the CLAUDE.md convention: every public function accepts ``as_of`` and
``dryrun_run_id``. ``as_of`` is the "now" used for trading-day calculations
(replay-friendly); ``dryrun_run_id`` scopes the attribution to a dry-run.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from loguru import logger
from sqlalchemy import and_, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db import session_scope
from src.models.fno_candidate import FNOCandidate
from src.models.fno_iv import IVHistory
from src.models.fno_signal import FNOSignal
from src.models.llm_decision_log import LLMDecisionLog


# Calibration target: outcome_z = realised_pnl_pct / expected_vol_at_entry.
# `expected_vol_at_entry` here uses the latest available iv_history.rv_20d
# (annualised) scaled to the observed holding window. Trading days per year
# = 252, so per-day vol = annual / sqrt(252).
_TRADING_DAYS_PER_YEAR = 252

# Counterfactual window: v9 SKIPs that don't reach T+5 trading days are not
# yet eligible for an "unobservable" verdict — leave them pending so a late
# fno_signal still gets attributed if one is opened.
_COUNTERFACTUAL_AGE_TRADING_DAYS = 5

# Hard ceiling: anything still open at T+30 trading days is "timeout".
_TIMEOUT_AGE_TRADING_DAYS = 30


@dataclass(frozen=True)
class AttributionResult:
    n_examined: int
    n_traded: int
    n_counterfactual: int
    n_unobservable: int
    n_timeout: int
    n_still_pending: int


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def attribute_llm_outcomes(
    *,
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
    batch_size: int = 500,
) -> AttributionResult:
    """Walk pending ``llm_decision_log`` rows and write outcomes where derivable.

    Returns counts by disposition for observability. Call this on a 5-minute
    cadence; the work is bounded by ``batch_size`` so a backlog cannot
    monopolise a tick.
    """
    now = as_of or datetime.now(tz=timezone.utc)
    today = now.date()
    n_traded = n_cf = n_un = n_to = n_pending = 0

    async with session_scope() as session:
        rows = await _load_pending(session, dryrun_run_id=dryrun_run_id, limit=batch_size)
        n_examined = len(rows)

        for row in rows:
            disposition = await _attribute_one(session, row=row, today=today, now=now)
            if disposition == "traded":
                n_traded += 1
            elif disposition == "counterfactual":
                n_cf += 1
            elif disposition == "unobservable":
                n_un += 1
            elif disposition == "timeout":
                n_to += 1
            else:
                n_pending += 1

    if n_examined:
        logger.info(
            "llm_outcomes: examined={} traded={} cf={} unobs={} timeout={} pending={}".format(
                n_examined, n_traded, n_cf, n_un, n_to, n_pending
            )
        )
    return AttributionResult(
        n_examined=n_examined,
        n_traded=n_traded,
        n_counterfactual=n_cf,
        n_unobservable=n_un,
        n_timeout=n_to,
        n_still_pending=n_pending,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _load_pending(
    session: AsyncSession,
    *,
    dryrun_run_id: uuid.UUID | None,
    limit: int,
) -> list[LLMDecisionLog]:
    """Return the oldest pending rows (oldest first → catch up backlog first)."""
    stmt = (
        select(LLMDecisionLog)
        .where(LLMDecisionLog.outcome_attributed_at.is_(None))
        .order_by(LLMDecisionLog.run_date.asc(), LLMDecisionLog.id.asc())
        .limit(limit)
    )
    if dryrun_run_id is None:
        stmt = stmt.where(LLMDecisionLog.dryrun_run_id.is_(None))
    else:
        stmt = stmt.where(LLMDecisionLog.dryrun_run_id == dryrun_run_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _attribute_one(
    session: AsyncSession, *, row: LLMDecisionLog, today: date, now: datetime
) -> str:
    """Attribute one llm_decision_log row. Returns its outcome_class
    (or 'pending' when no attribution is possible yet)."""

    age_trading_days = _trading_days_between(row.run_date, today)
    signal = await _find_signal_for_row(session, row=row)

    # --- Case 1: traded → closed ----------------------------------------
    if signal is not None and signal.status == "closed":
        pnl_pct = _safe_pnl_pct(signal)
        if pnl_pct is None:
            # Position closed but premiums are missing — record what we can
            # and mark unobservable so calibration does not consume garbage.
            await _persist(session, row=row, now=now, outcome_class="unobservable")
            return "unobservable"

        expected_vol = await _expected_vol_at_entry(session, row=row, signal=signal)
        outcome_z = (pnl_pct / expected_vol) if (expected_vol and expected_vol > 0) else None
        await _persist(
            session, row=row, now=now,
            outcome_class="traded",
            outcome_pnl_pct=pnl_pct,
            outcome_z=outcome_z,
        )
        return "traded"

    # --- Case 2: open but stale → timeout -------------------------------
    if age_trading_days >= _TIMEOUT_AGE_TRADING_DAYS:
        await _persist(session, row=row, now=now, outcome_class="timeout")
        return "timeout"

    # --- Case 3: never opened, past T+5 → counterfactual or unobservable
    if signal is None and age_trading_days >= _COUNTERFACTUAL_AGE_TRADING_DAYS:
        cf_pnl = await _counterfactual_pnl_pct(session, row=row, today=today)
        if cf_pnl is None:
            await _persist(session, row=row, now=now, outcome_class="unobservable")
            return "unobservable"
        expected_vol = await _expected_vol_at_entry(session, row=row, signal=None)
        outcome_z = (cf_pnl / expected_vol) if (expected_vol and expected_vol > 0) else None
        await _persist(
            session, row=row, now=now,
            outcome_class="counterfactual",
            outcome_pnl_pct=cf_pnl,
            outcome_z=outcome_z,
        )
        return "counterfactual"

    # --- Case 4: still pending — leave alone, will be revisited ---------
    return "pending"


async def _find_signal_for_row(
    session: AsyncSession, *, row: LLMDecisionLog
) -> FNOSignal | None:
    """Locate the downstream fno_signal for this LLM call.

    Lineage: llm_decision_log.run_date+instrument_id → fno_candidates (phase=3)
    → fno_signals.candidate_id. There is at most one phase=3 candidate per
    (instrument_id, run_date, dryrun_run_id).

    The ``dryrun_run_id`` predicate on BOTH joins is critical (review fix
    P0 #2): without it a dry-run replay landing the same (run_date,
    instrument_id) would be picked non-deterministically by ``.limit(1)``,
    causing live LLM-log rows to be attributed against dry-run P&L (or
    vice versa).

    Picks the most recent signal when multiple share the candidate (rare —
    can happen if an entry was rejected and re-proposed; we attribute to the
    latest because that's what the operator ultimately acted on).
    """
    cand_predicates = [
        FNOCandidate.run_date == row.run_date,
        FNOCandidate.instrument_id == row.instrument_id,
        FNOCandidate.phase == 3,
    ]
    if row.dryrun_run_id is None:
        cand_predicates.append(FNOCandidate.dryrun_run_id.is_(None))
    else:
        cand_predicates.append(FNOCandidate.dryrun_run_id == row.dryrun_run_id)

    cand_stmt = select(FNOCandidate.id).where(*cand_predicates).limit(1)
    cand_id = (await session.execute(cand_stmt)).scalar_one_or_none()
    if cand_id is None:
        return None

    sig_predicates = [FNOSignal.candidate_id == cand_id]
    if row.dryrun_run_id is None:
        sig_predicates.append(FNOSignal.dryrun_run_id.is_(None))
    else:
        sig_predicates.append(FNOSignal.dryrun_run_id == row.dryrun_run_id)

    sig_stmt = (
        select(FNOSignal)
        .where(*sig_predicates)
        .order_by(FNOSignal.proposed_at.desc())
        .limit(1)
    )
    return (await session.execute(sig_stmt)).scalar_one_or_none()


def _safe_pnl_pct(signal: FNOSignal) -> float | None:
    """Realised P&L as a fraction of risked premium.

    Uses ``final_pnl / entry_premium_net``. ``entry_premium_net`` is the
    structure's net debit/credit at fill — the right denominator for both
    debit spreads (net premium paid) and credit structures (net premium
    received, where final_pnl can exceed the credit on a profitable close).
    Returns None when either value is missing.
    """
    if signal.final_pnl is None or signal.entry_premium_net is None:
        return None
    entry = float(signal.entry_premium_net)
    if entry == 0.0:
        return None
    return float(signal.final_pnl) / abs(entry)


async def _counterfactual_pnl_pct(
    session: AsyncSession, *, row: LLMDecisionLog, today: date
) -> float | None:
    """Counterfactual P&L for a never-opened v10 call.

    Plan reference: §0.3 / S6. Prices the v10 prompt's ``proposed_structure``
    off ``options_chain`` snapshots at run_date (entry) and at
    ``min(run_date + 5 trading days, proposed_expiry - 1)`` (exit).

    Quality gates that route the row to 'unobservable' (caller marks the
    outcome accordingly):
      - v9 row (no strikes emitted)
      - missing structure / strikes / expiry
      - expiry already past at log write time
      - any leg's bid-ask spread > 10% at entry or exit
      - any leg has no quote within ±30 minutes of the snapshot window

    Returns a signed percentage: positive = the trade would have made money,
    negative = lost. Uses mid-quote pricing throughout.
    """
    if not row.prompt_version or not row.prompt_version.startswith("v10"):
        return None
    raw = row.raw_response or {}
    strikes_raw = raw.get("proposed_strikes")
    expiry_raw = raw.get("proposed_expiry")
    structure = (raw.get("proposed_structure") or "").strip().lower()
    conviction = raw.get("directional_conviction")
    if not strikes_raw or not expiry_raw or not structure:
        return None
    try:
        strikes = [float(s) for s in strikes_raw]
        expiry = date.fromisoformat(str(expiry_raw))
    except (ValueError, TypeError):
        return None

    # Legs are inferred from structure + conviction sign. Each leg is
    # (option_type, strike, sign) where sign is +1 for long (we paid),
    # -1 for short (we received).
    legs = _legs_for_structure(structure, strikes, conviction)
    if not legs:
        return None

    # Exit date capped at the day BEFORE expiry so we can still price.
    exit_date_cap = expiry - timedelta(days=1)
    exit_date = row.run_date + timedelta(days=_calendar_days_for_trading(_COUNTERFACTUAL_AGE_TRADING_DAYS))
    exit_date = min(exit_date, exit_date_cap)
    if exit_date <= row.run_date:
        return None

    # Entry is priced at the FIRST market-hours snapshot of run_date so
    # the counterfactual P&L captures the same-session move the LLM was
    # forecasting; exit is priced at the LAST snapshot of exit_date so
    # the holding window matches a realistic close (review fix P1 #3).
    entry_premium = await _price_legs(
        session, instrument_id=row.instrument_id,
        legs=legs, expiry=expiry, on_date=row.run_date,
        which="first",
    )
    if entry_premium is None:
        return None
    exit_premium = await _price_legs(
        session, instrument_id=row.instrument_id,
        legs=legs, expiry=expiry, on_date=exit_date,
        which="last",
    )
    if exit_premium is None:
        return None

    if entry_premium == 0:
        return None
    return float(exit_premium - entry_premium) / abs(float(entry_premium))


def _legs_for_structure(
    structure: str, strikes: list[float], conviction: float | None
) -> list[tuple[str, float, int]] | None:
    """Map a structure name + strike list to a list of (type, strike, sign) legs.

    Returns None for unsupported structures — the caller will mark
    'unobservable'. We support the structures the v10 prompt is told to
    emit (see ``FNO_THESIS_SYSTEM_V10``).
    """
    sorted_strikes = sorted(strikes)
    n = len(sorted_strikes)

    if structure == "long_call" and n >= 1:
        return [("CE", sorted_strikes[0], +1)]
    if structure == "long_put" and n >= 1:
        return [("PE", sorted_strikes[0], +1)]

    if structure == "bull_call_spread" and n >= 2:
        return [("CE", sorted_strikes[0], +1), ("CE", sorted_strikes[-1], -1)]
    if structure == "bear_put_spread" and n >= 2:
        return [("PE", sorted_strikes[-1], +1), ("PE", sorted_strikes[0], -1)]
    if structure == "bull_put_spread" and n >= 2:
        return [("PE", sorted_strikes[-1], -1), ("PE", sorted_strikes[0], +1)]
    if structure == "bear_call_spread" and n >= 2:
        return [("CE", sorted_strikes[0], -1), ("CE", sorted_strikes[-1], +1)]

    if structure == "long_straddle" and n >= 1:
        strike = sorted_strikes[n // 2]
        return [("CE", strike, +1), ("PE", strike, +1)]
    if structure == "short_strangle" and n >= 2:
        return [("CE", sorted_strikes[-1], -1), ("PE", sorted_strikes[0], -1)]

    if structure == "iron_condor" and n >= 4:
        # Convention: [put_long, put_short, call_short, call_long]
        return [
            ("PE", sorted_strikes[0], +1),
            ("PE", sorted_strikes[1], -1),
            ("CE", sorted_strikes[2], -1),
            ("CE", sorted_strikes[3], +1),
        ]

    # Fallback: single strike + sign(conviction) → call/put long.
    if n == 1 and conviction is not None:
        try:
            opt = "CE" if float(conviction) >= 0 else "PE"
            return [(opt, sorted_strikes[0], +1)]
        except (TypeError, ValueError):
            return None
    return None


async def _price_legs(
    session: AsyncSession,
    *,
    instrument_id,
    legs: list[tuple[str, float, int]],
    expiry: date,
    on_date: date,
    which: str = "last",
) -> float | None:
    """Return the net mid-quote premium for the leg set on ``on_date``.

    For each leg: take a chain snapshot from the market-hours window on
    ``on_date`` for (instrument, expiry, strike, option_type). ``which``
    controls which snapshot is used:

      - ``"first"``: the earliest snapshot (≈ 09:15 IST open). The right
        choice for an "entry" valuation — captures the price the
        counterfactual would have transacted at.
      - ``"last"``:  the latest snapshot (≈ 15:30 IST close). The right
        choice for an "exit" valuation.

    Reject the whole valuation if any leg's bid-ask spread > 10% of mid
    or both bid/ask AND ltp are missing (plan §0.3 quality gate).

    The window spans 08:30 IST → 16:30 IST (03:00 → 11:00 UTC) of
    ``on_date`` — wide enough to cover pre-market warm-up snapshots and
    delayed EOD chain writes.
    """
    if which not in ("first", "last"):
        raise ValueError(f"_price_legs: which must be 'first' or 'last', got {which!r}")

    snapshot_lower = datetime.combine(on_date, time(3, 0, tzinfo=timezone.utc))
    snapshot_upper = datetime.combine(on_date, time(11, 0, tzinfo=timezone.utc))
    order_direction = "ASC" if which == "first" else "DESC"

    total = 0.0
    for opt_type, strike, sign in legs:
        # ``order_direction`` is interpolated rather than bound because
        # SQL ORDER BY doesn't accept bound parameters. The value is
        # constrained to the two literal options above so this is
        # injection-safe.
        row = (await session.execute(
            text(f"""
                SELECT bid_price, ask_price, ltp
                FROM options_chain
                WHERE instrument_id = :inst
                  AND expiry_date   = :expiry
                  AND strike_price  = :strike
                  AND option_type   = :opt
                  AND snapshot_at  >= :lo
                  AND snapshot_at  <= :hi
                ORDER BY snapshot_at {order_direction}
                LIMIT 1
            """),
            {
                "inst": str(instrument_id),
                "expiry": expiry,
                "strike": strike,
                "opt": opt_type,
                "lo": snapshot_lower,
                "hi": snapshot_upper,
            },
        )).first()
        if row is None:
            return None
        bid = float(row.bid_price) if row.bid_price is not None else None
        ask = float(row.ask_price) if row.ask_price is not None else None
        ltp = float(row.ltp) if row.ltp is not None else None
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            # Fall back to LTP only when both bid and ask are missing.
            if ltp is None or ltp <= 0:
                return None
            mid = ltp
        else:
            mid = (bid + ask) / 2.0
            spread_pct = (ask - bid) / mid if mid > 0 else 1.0
            if spread_pct > 0.10:
                return None
        total += sign * mid
    return total


def _calendar_days_for_trading(trading_days: int) -> int:
    """Rough calendar-day count for ``trading_days`` weekdays.

    A trading week is 5 weekdays in 7 calendar days, so 5 trading days
    ≈ 7 calendar days. The mapping is approximate (holidays) but the
    eventual snapshot lookup uses a 6-hour window so a 1-day drift is
    absorbed without changing semantics.
    """
    return int(round(trading_days * 1.4))


async def _expected_vol_at_entry(
    session: AsyncSession,
    *,
    row: LLMDecisionLog,
    signal: FNOSignal | None,
) -> float | None:
    """Per-position σ used to z-score the realised P&L.

    σ_position = (rv_annual at entry) × √(holding_days / 252)

    Preference order for ``rv_annual`` (review fix P1 #5):
      1. ``signal.rv_annualised_at_entry`` — the entry-executor snapshotted
         feature_store.rv_30min (intraday) or iv_history.rv_20d at entry.
      2. ``iv_history.rv_20d`` for the run_date — legacy / counterfactual.

    Holding window:
      - Traded rows: observed (closed_at - filled_at), floored at 1 trading hour.
      - Counterfactuals: 5-trading-day assumption (matches the counterfactual
        window from §0.3).
    """
    rv_annual: float | None = None
    if signal is not None and signal.rv_annualised_at_entry is not None:
        try:
            rv_annual = float(signal.rv_annualised_at_entry)
        except (TypeError, ValueError):
            rv_annual = None

    if rv_annual is None:
        rv_stmt = (
            select(IVHistory.rv_20d)
            .where(
                IVHistory.instrument_id == row.instrument_id,
                IVHistory.date <= row.run_date,
                IVHistory.rv_20d.isnot(None),
            )
            .order_by(IVHistory.date.desc())
            .limit(1)
        )
        rv_20d_dec = (await session.execute(rv_stmt)).scalar_one_or_none()
        if rv_20d_dec is None:
            return None
        rv_annual = float(rv_20d_dec)

    if signal is not None and signal.filled_at and signal.closed_at:
        holding_seconds = (signal.closed_at - signal.filled_at).total_seconds()
        holding_days = max(holding_seconds / 86400.0, 1.0 / 6.5)  # floor 1 trading hour
    else:
        holding_days = float(_COUNTERFACTUAL_AGE_TRADING_DAYS)

    return rv_annual * (holding_days / _TRADING_DAYS_PER_YEAR) ** 0.5


async def _persist(
    session: AsyncSession,
    *,
    row: LLMDecisionLog,
    now: datetime,
    outcome_class: str,
    outcome_pnl_pct: float | None = None,
    outcome_z: float | None = None,
) -> None:
    """Write outcome fields back to llm_decision_log."""
    stmt = (
        update(LLMDecisionLog)
        .where(LLMDecisionLog.id == row.id)
        .values(
            outcome_class=outcome_class,
            outcome_pnl_pct=outcome_pnl_pct,
            outcome_z=outcome_z,
            outcome_attributed_at=now,
        )
    )
    await session.execute(stmt)


def _trading_days_between(start: date, end: date) -> int:
    """Approximate count of Mon–Fri days between ``start`` and ``end``.

    Holidays are not subtracted — the cost is one extra pending poll per
    holiday, which is harmless. Negative spans return 0.
    """
    if end <= start:
        return 0
    delta = (end - start).days
    full_weeks, leftover = divmod(delta, 7)
    weekdays = full_weeks * 5
    # walk the leftover days starting from start.weekday()+1
    wd = start.weekday()
    for i in range(1, leftover + 1):
        if (wd + i) % 7 < 5:
            weekdays += 1
    return weekdays
