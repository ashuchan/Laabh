"""Volatility Surface — IV skew, term structure, and OI walls from options chain data.

What this computes (and why these methods, not the "textbook" alternatives):

  SKEW (moneyness-based, not delta-based):
    The delta values stored in options_chain are unreliable — chain sources
    (NSE/Dhan) provide pre-computed Greeks with an unverified scaling convention
    that produces CE delta ≈ 0.99 at near-ATM strikes (should be ~0.50-0.65).
    Using stored deltas would give wrong 25Δ strike identification.
    Instead: find the highest-OI strike in the 2-15% OTM band on each side.
    This is model-free, robust to chain sparsity, and finds where the market
    is actually positioned (not a theoretical delta level).

  TERM STRUCTURE (ATM IV comparison):
    ATM IV is the most liquid and cleanest IV reading in any chain. Comparing
    front vs. back month ATM IV gives the term structure slope without requiring
    full surface interpolation.

  OI WALLS (not gamma-weighted GEX):
    All lot_size values in the instruments table are 1 (default — never populated).
    Absolute GEX in crores requires correct lot sizes, so we skip it entirely.
    OI walls (pin strike, call wall, put wall) are computed purely from raw OI,
    which is reliably stored. These are the standard "max pain" inputs used by
    Indian retail options traders and are more actionable for condor wing placement.

Integration:
    - Called in run_premarket_pipeline() after Phase 2.5 fan-in, before Phase 3.
    - thesis_synthesizer reads via get_latest_surface().
    - Prompts v7: adds surface context to LLM as vol_surface_block.
    - EOD: also called from orchestrator.run_eod_tasks() to archive the session surface.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Sequence

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.db import session_scope
from src.models.fno_chain import OptionsChain
from src.models.fno_vol_surface import VolSurfaceSnapshot
from src.models.instrument import Instrument


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _ChainRow:
    """Flat representation of one options_chain row for pure computation."""
    strike: float
    option_type: str    # 'CE' or 'PE'
    iv: float | None
    oi: int
    expiry: date


@dataclass
class SurfaceResult:
    """Full vol surface reading for one instrument on one date."""
    instrument_id: str
    symbol: str
    run_date: date
    chain_snap_at: datetime | None = None

    # Skew
    iv_skew_5pct: float | None = None       # iv_otm_put - iv_otm_call (% points)
    iv_otm_put: float | None = None
    iv_otm_call: float | None = None
    otm_put_strike: float | None = None
    otm_call_strike: float | None = None
    skew_regime: str = "insufficient_data"

    # Term structure
    expiry_near: date | None = None
    expiry_far: date | None = None
    iv_front: float | None = None
    iv_back: float | None = None
    term_slope: float | None = None          # iv_back - iv_front (% points)
    term_regime: str = "single_expiry"

    # OI walls
    pin_strike: float | None = None          # argmax(CE_OI + PE_OI)
    call_wall: float | None = None           # highest CE OI strike
    put_wall: float | None = None            # highest PE OI strike
    pcr_near_expiry: float | None = None     # total PE OI / CE OI for near expiry
    underlying_ltp: float | None = None
    days_to_expiry: int | None = None

    def as_prompt_block(self) -> str:
        """Render a one-line summary for the Phase 3 LLM prompt."""
        parts: list[str] = []

        # Skew
        if self.iv_skew_5pct is not None:
            skew_str = f"{self.iv_skew_5pct:+.1f}vpts"
            put_str = (f"put({self.otm_put_strike:.0f})={self.iv_otm_put:.1f}%"
                       if self.iv_otm_put is not None and self.otm_put_strike is not None else "")
            call_str = (f"call({self.otm_call_strike:.0f})={self.iv_otm_call:.1f}%"
                        if self.iv_otm_call is not None and self.otm_call_strike is not None else "")
            parts.append(f"Skew={self.skew_regime.upper()} ({skew_str}: {put_str}, {call_str})")
        else:
            parts.append("Skew=insufficient_data")

        # Term structure
        if self.term_slope is not None:
            parts.append(
                f"Term={self.term_regime.upper()} "
                f"(front={self.iv_front:.1f}%, back={self.iv_back:.1f}%, slope={self.term_slope:+.1f}vpts)"
            )
        else:
            parts.append(f"Term={self.term_regime.upper()}")

        # OI walls
        oi_parts = []
        if self.pin_strike:
            oi_parts.append(f"pin={self.pin_strike:.0f}")
        if self.call_wall:
            oi_parts.append(f"call_wall={self.call_wall:.0f}")
        if self.put_wall:
            oi_parts.append(f"put_wall={self.put_wall:.0f}")
        if self.pcr_near_expiry is not None:
            oi_parts.append(f"PCR={self.pcr_near_expiry:.2f}")
        if oi_parts:
            parts.append(f"OI_walls=({', '.join(oi_parts)})")

        return " | ".join(parts)


# ---------------------------------------------------------------------------
# Pure computation helpers (no I/O)
# ---------------------------------------------------------------------------

def compute_skew(
    rows: Sequence[_ChainRow],
    underlying: float,
    expiry: date,
    *,
    min_otm_pct: float = 0.02,
    max_otm_pct: float = 0.15,
    skew_put_threshold: float = 1.5,
    skew_call_threshold: float = -1.5,
) -> tuple[float | None, float | None, float | None, float | None, str]:
    """Find the highest-OI OTM strike on each side and compute skew.

    Searches the band [spot × (1 - max_otm), spot × (1 - min_otm)] for puts
    and [spot × (1 + min_otm), spot × (1 + max_otm)] for calls.
    Within each band, picks the strike with the highest OI.

    Returns (iv_otm_put, otm_put_strike, iv_otm_call, otm_call_strike, regime).
    Any component may be None when insufficient chain data exists.
    """
    near_rows = [r for r in rows if r.expiry == expiry]
    if not near_rows or underlying <= 0:
        return None, None, None, None, "insufficient_data"

    put_band_lo = underlying * (1 - max_otm_pct)
    put_band_hi = underlying * (1 - min_otm_pct)
    call_band_lo = underlying * (1 + min_otm_pct)
    call_band_hi = underlying * (1 + max_otm_pct)

    # Candidate puts: strikes in band, with non-null IV and OI > 0
    put_candidates = [
        r for r in near_rows
        if r.option_type == "PE"
        and put_band_lo <= r.strike <= put_band_hi
        and r.iv is not None and r.iv > 0
        and r.oi > 0
    ]
    call_candidates = [
        r for r in near_rows
        if r.option_type == "CE"
        and call_band_lo <= r.strike <= call_band_hi
        and r.iv is not None and r.iv > 0
        and r.oi > 0
    ]

    iv_put = otm_put = None
    iv_call = otm_call = None

    if put_candidates:
        best_put = max(put_candidates, key=lambda r: r.oi)
        iv_put = best_put.iv
        otm_put = best_put.strike

    if call_candidates:
        best_call = max(call_candidates, key=lambda r: r.oi)
        iv_call = best_call.iv
        otm_call = best_call.strike

    if iv_put is None or iv_call is None:
        return iv_put, otm_put, iv_call, otm_call, "insufficient_data"

    skew = iv_put - iv_call
    if skew > skew_put_threshold:
        regime = "put_skewed"
    elif skew < skew_call_threshold:
        regime = "call_skewed"
    else:
        regime = "flat"

    return iv_put, otm_put, iv_call, otm_call, regime


def compute_term_structure(
    rows: Sequence[_ChainRow],
    underlying: float,
    expiries: Sequence[date],
    run_date: date,
    *,
    min_dte_for_front: int = 4,
    normal_threshold: float = 0.5,
    inverted_threshold: float = -0.5,
) -> tuple[date | None, date | None, float | None, float | None, float | None, str]:
    """Compute ATM IV for front and back expiry and derive term structure slope.

    Returns (expiry_near, expiry_far, iv_front, iv_back, slope, regime).

    Near-pin guard: if front expiry has < min_dte_for_front days to run_date,
    it is treated as a special near-pin expiry and the NEXT expiry becomes
    the term-structure front. The near-pin expiry is still reported as
    expiry_near but regime is set to 'near_pin'.
    """
    if len(expiries) < 2:
        near = expiries[0] if expiries else None
        iv_front = _atm_iv_for_expiry(rows, underlying, near) if near else None
        return near, None, iv_front, None, None, "single_expiry"

    front, back = expiries[0], expiries[1]
    dte_front = (front - run_date).days

    near_pin = dte_front < min_dte_for_front

    iv_front = _atm_iv_for_expiry(rows, underlying, front)
    iv_back = _atm_iv_for_expiry(rows, underlying, back)

    if iv_front is None or iv_back is None:
        return front, back, iv_front, iv_back, None, "single_expiry"

    slope = iv_back - iv_front

    if near_pin:
        regime = "near_pin"
    elif slope > normal_threshold:
        regime = "normal"
    elif slope < inverted_threshold:
        regime = "inverted"
    else:
        regime = "flat"

    return front, back, iv_front, iv_back, slope, regime


def compute_oi_walls(
    rows: Sequence[_ChainRow],
    expiry: date,
) -> tuple[float | None, float | None, float | None, float | None]:
    """Compute pin strike, call wall, put wall, and near-expiry PCR.

    Returns (pin_strike, call_wall, put_wall, pcr).

    pin_strike: argmax(CE_OI + PE_OI) — where OI is most concentrated (max pain proxy).
    call_wall:  strike with highest CE OI (resistance level for spot).
    put_wall:   strike with highest PE OI (support level for spot).
    pcr:        total PE OI / total CE OI for the near expiry (market direction proxy).
    """
    near_rows = [r for r in rows if r.expiry == expiry and r.oi > 0]
    if not near_rows:
        return None, None, None, None

    # Build per-strike totals
    ce_oi: dict[float, int] = {}
    pe_oi: dict[float, int] = {}
    for r in near_rows:
        if r.option_type == "CE":
            ce_oi[r.strike] = ce_oi.get(r.strike, 0) + r.oi
        elif r.option_type == "PE":
            pe_oi[r.strike] = pe_oi.get(r.strike, 0) + r.oi

    all_strikes = set(ce_oi) | set(pe_oi)
    if not all_strikes:
        return None, None, None, None

    pin_strike = max(all_strikes, key=lambda k: ce_oi.get(k, 0) + pe_oi.get(k, 0))
    call_wall = max(ce_oi, key=ce_oi.get) if ce_oi else None
    put_wall = max(pe_oi, key=pe_oi.get) if pe_oi else None

    total_ce = sum(ce_oi.values())
    total_pe = sum(pe_oi.values())
    pcr = round(total_pe / total_ce, 4) if total_ce > 0 else None

    return pin_strike, call_wall, put_wall, pcr


def _atm_iv_for_expiry(
    rows: Sequence[_ChainRow],
    underlying: float,
    expiry: date | None,
) -> float | None:
    """Average CE + PE IV at the nearest strike to underlying for a given expiry."""
    if expiry is None or underlying <= 0:
        return None
    exp_rows = [r for r in rows if r.expiry == expiry and r.iv is not None and r.iv > 0]
    if not exp_rows:
        return None
    strikes = sorted({r.strike for r in exp_rows})
    atm = min(strikes, key=lambda s: abs(s - underlying))
    ivs = [r.iv for r in exp_rows if r.strike == atm]
    return round(sum(ivs) / len(ivs), 4) if ivs else None


def compute_surface(
    rows: Sequence[_ChainRow],
    underlying: float,
    run_date: date,
) -> dict:
    """Pure function: given chain rows and spot, return all surface metrics.

    Separates computation from I/O for full testability.
    Returns a dict with all SurfaceResult fields (excluding instrument_id, symbol, chain_snap_at).
    """
    expiries = sorted({r.expiry for r in rows if r.expiry >= run_date})
    near_expiry = expiries[0] if expiries else None

    # Skew
    iv_put, put_k, iv_call, call_k, skew_regime = compute_skew(
        rows, underlying, near_expiry
    ) if near_expiry else (None, None, None, None, "insufficient_data")

    skew_val = round(iv_put - iv_call, 4) if (iv_put and iv_call) else None

    # Term structure
    exp_near, exp_far, iv_front, iv_back, slope, term_regime = compute_term_structure(
        rows, underlying, expiries, run_date
    )

    # OI walls (nearest expiry)
    pin, call_wall, put_wall, pcr = compute_oi_walls(rows, near_expiry) if near_expiry else (None, None, None, None)

    raw_dte = (near_expiry - run_date).days if near_expiry else None
    dte = max(0, raw_dte) if raw_dte is not None else None   # guard against stale chain data

    return dict(
        iv_skew_5pct=skew_val,
        iv_otm_put=iv_put,
        iv_otm_call=iv_call,
        otm_put_strike=put_k,
        otm_call_strike=call_k,
        skew_regime=skew_regime,
        expiry_near=exp_near,
        expiry_far=exp_far,
        iv_front=iv_front,
        iv_back=iv_back,
        term_slope=slope,
        term_regime=term_regime,
        pin_strike=pin,
        call_wall=call_wall,
        put_wall=put_wall,
        pcr_near_expiry=pcr,
        underlying_ltp=underlying,
        days_to_expiry=dte,
    )


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _get_chain_rows(
    session,
    instrument_id: str,
    run_date: date,
) -> tuple[list[_ChainRow], datetime | None, float | None]:
    """Fetch the latest chain snapshot for instrument on run_date.

    Returns (rows, snapshot_at, underlying_ltp).
    Uses per-instrument latest snapshot (not global max) so partial intraday
    runs don't collapse to a single instrument.
    """
    snap_subq = (
        select(func.max(OptionsChain.snapshot_at))
        .where(
            OptionsChain.instrument_id == instrument_id,
            func.date(OptionsChain.snapshot_at) == run_date,
        )
        .scalar_subquery()
    )

    result = await session.execute(
        select(
            OptionsChain.strike_price,
            OptionsChain.option_type,
            OptionsChain.iv,
            OptionsChain.oi,
            OptionsChain.expiry_date,
            OptionsChain.underlying_ltp,
            OptionsChain.snapshot_at,
        ).where(
            OptionsChain.instrument_id == instrument_id,
            OptionsChain.snapshot_at == snap_subq,
            OptionsChain.oi.isnot(None),
        )
    )
    db_rows = result.all()
    if not db_rows:
        return [], None, None

    snap_at = db_rows[0].snapshot_at
    underlying = float(db_rows[0].underlying_ltp) if db_rows[0].underlying_ltp else None

    chain_rows = [
        _ChainRow(
            strike=float(r.strike_price),
            option_type=r.option_type,
            iv=float(r.iv) if r.iv is not None else None,
            oi=int(r.oi),
            expiry=r.expiry_date,
        )
        for r in db_rows
    ]
    return chain_rows, snap_at, underlying


async def _upsert_surface(session, inst_id: str, run_date: date, snap_at, metrics: dict) -> None:
    """Upsert surface snapshot. ON CONFLICT (instrument_id, run_date) DO UPDATE."""

    def _dec(v):
        return Decimal(str(round(v, 6))) if v is not None else None

    stmt = pg_insert(VolSurfaceSnapshot).values(
        instrument_id=inst_id,
        run_date=run_date,
        chain_snap_at=snap_at,
        iv_skew_5pct=_dec(metrics.get("iv_skew_5pct")),
        iv_otm_put=_dec(metrics.get("iv_otm_put")),
        iv_otm_call=_dec(metrics.get("iv_otm_call")),
        otm_put_strike=_dec(metrics.get("otm_put_strike")),
        otm_call_strike=_dec(metrics.get("otm_call_strike")),
        skew_regime=metrics.get("skew_regime"),
        expiry_near=metrics.get("expiry_near"),
        expiry_far=metrics.get("expiry_far"),
        iv_front=_dec(metrics.get("iv_front")),
        iv_back=_dec(metrics.get("iv_back")),
        term_slope=_dec(metrics.get("term_slope")),
        term_regime=metrics.get("term_regime"),
        pin_strike=_dec(metrics.get("pin_strike")),
        call_wall=_dec(metrics.get("call_wall")),
        put_wall=_dec(metrics.get("put_wall")),
        pcr_near_expiry=_dec(metrics.get("pcr_near_expiry")),
        underlying_ltp=_dec(metrics.get("underlying_ltp")),
        days_to_expiry=metrics.get("days_to_expiry"),
    ).on_conflict_do_update(
        index_elements=["instrument_id", "run_date"],
        set_={k: v for k, v in {
            "chain_snap_at": snap_at,
            "iv_skew_5pct": _dec(metrics.get("iv_skew_5pct")),
            "iv_otm_put": _dec(metrics.get("iv_otm_put")),
            "iv_otm_call": _dec(metrics.get("iv_otm_call")),
            "otm_put_strike": _dec(metrics.get("otm_put_strike")),
            "otm_call_strike": _dec(metrics.get("otm_call_strike")),
            "skew_regime": metrics.get("skew_regime"),
            "expiry_near": metrics.get("expiry_near"),
            "expiry_far": metrics.get("expiry_far"),
            "iv_front": _dec(metrics.get("iv_front")),
            "iv_back": _dec(metrics.get("iv_back")),
            "term_slope": _dec(metrics.get("term_slope")),
            "term_regime": metrics.get("term_regime"),
            "pin_strike": _dec(metrics.get("pin_strike")),
            "call_wall": _dec(metrics.get("call_wall")),
            "put_wall": _dec(metrics.get("put_wall")),
            "pcr_near_expiry": _dec(metrics.get("pcr_near_expiry")),
            "underlying_ltp": _dec(metrics.get("underlying_ltp")),
            "days_to_expiry": metrics.get("days_to_expiry"),
        }.items() if v is not None or k in ("skew_regime", "term_regime")},
    )
    await session.execute(stmt)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def compute_for_instruments(
    run_date: date | None = None,
    instrument_ids: list[str] | None = None,
) -> int:
    """Compute and persist vol surface for F&O instruments on run_date.

    When instrument_ids is provided (e.g., Phase-2 passers), only those
    instruments are processed. When None, all active F&O instruments are used.

    Returns the count of snapshots written.
    """
    if run_date is None:
        from datetime import date as _date
        run_date = _date.today()

    if instrument_ids is None:
        async with session_scope() as session:
            result = await session.execute(
                select(Instrument.id, Instrument.symbol).where(
                    Instrument.is_fno == True,   # noqa: E712
                    Instrument.is_active == True,
                )
            )
            instruments = [(str(r.id), r.symbol) for r in result.all()]
    else:
        async with session_scope() as session:
            result = await session.execute(
                select(Instrument.id, Instrument.symbol).where(
                    Instrument.id.in_(instrument_ids)
                )
            )
            instruments = [(str(r.id), r.symbol) for r in result.all()]

    written = skipped = 0

    for inst_id, symbol in instruments:
        try:
            async with session_scope() as session:
                chain_rows, snap_at, underlying = await _get_chain_rows(session, inst_id, run_date)

            if not chain_rows or underlying is None or underlying <= 0:
                skipped += 1
                continue

            metrics = compute_surface(chain_rows, underlying, run_date)

            async with session_scope() as session:
                await _upsert_surface(session, inst_id, run_date, snap_at, metrics)

            written += 1
            skew_str = f"{metrics['iv_skew_5pct']:+.1f}" if metrics['iv_skew_5pct'] is not None else "n/a"
            logger.debug(
                f"vol_surface: {symbol} skew={metrics['skew_regime']} "
                f"({skew_str}vpts) "
                f"term={metrics['term_regime']} "
                f"pin={metrics['pin_strike']}"
            )

        except Exception as exc:
            logger.warning(f"vol_surface: {symbol} failed: {exc!r}")

    logger.info(f"vol_surface: {run_date} — written={written} skipped={skipped}")
    return written


async def get_latest_surface(
    instrument_id: str,
    run_date: date | None = None,
) -> SurfaceResult | None:
    """Return the most recent surface snapshot for an instrument.

    Returns None when no snapshot exists — Phase 3 renders "(surface unavailable)".
    """
    if run_date is None:
        from datetime import date as _date
        run_date = _date.today()

    async with session_scope() as session:
        row = (await session.execute(
            select(VolSurfaceSnapshot)
            .where(
                VolSurfaceSnapshot.instrument_id == instrument_id,
                VolSurfaceSnapshot.run_date <= run_date,
                VolSurfaceSnapshot.dryrun_run_id.is_(None),
            )
            .order_by(VolSurfaceSnapshot.run_date.desc())
            .limit(1)
        )).scalar_one_or_none()

        if row is None:
            return None

        sym = (await session.execute(
            select(Instrument.symbol).where(Instrument.id == instrument_id)
        )).scalar_one_or_none()

    def _f(v):
        return float(v) if v is not None else None

    return SurfaceResult(
        instrument_id=instrument_id,
        symbol=sym or "UNKNOWN",
        run_date=row.run_date,
        chain_snap_at=row.chain_snap_at,
        iv_skew_5pct=_f(row.iv_skew_5pct),
        iv_otm_put=_f(row.iv_otm_put),
        iv_otm_call=_f(row.iv_otm_call),
        otm_put_strike=_f(row.otm_put_strike),
        otm_call_strike=_f(row.otm_call_strike),
        skew_regime=row.skew_regime or "insufficient_data",
        expiry_near=row.expiry_near,
        expiry_far=row.expiry_far,
        iv_front=_f(row.iv_front),
        iv_back=_f(row.iv_back),
        term_slope=_f(row.term_slope),
        term_regime=row.term_regime or "single_expiry",
        pin_strike=_f(row.pin_strike),
        call_wall=_f(row.call_wall),
        put_wall=_f(row.put_wall),
        pcr_near_expiry=_f(row.pcr_near_expiry),
        underlying_ltp=_f(row.underlying_ltp),
        days_to_expiry=row.days_to_expiry,
    )
