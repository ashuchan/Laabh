"""F&O Thesis Synthesizer — Phase 3 LLM-based trade thesis generation.

Runs after Phase 2 (catalyst scoring). For each Phase-2 passing instrument,
calls Claude to synthesize a trade thesis and records:
  - llm_decision: PROCEED | SKIP | HEDGE
  - llm_thesis: reasoning paragraph
  - iv_regime and oi_structure from chain data

Writes audit log entries via llm_audit_log for every API call.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import anthropic
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.config import Settings
from src.db import session_scope
from src.fno.calendar import next_weekly_expiry
from src.fno.catalyst_scorer import get_latest_fii_dii
from src.fno.prompts import (
    FNO_THESIS_PROMPT_VERSION,
    FNO_THESIS_SYSTEM,
    FNO_THESIS_USER_TEMPLATE,
)
from src.fno.vix_collector import classify_regime
from src.models.fno_candidate import FNOCandidate
from src.models.instrument import Instrument
from src.models.llm_audit_log import LLMAuditLog
from src.models.signal import Signal

_settings = Settings()
_CALLER = "fno.thesis_synthesizer"


# ---------------------------------------------------------------------------
# Pure helpers (no I/O)
# ---------------------------------------------------------------------------

@dataclass
class ThesisResult:
    instrument_id: str
    symbol: str
    decision: str  # PROCEED | SKIP | HEDGE
    direction: str  # bullish | bearish | neutral
    thesis: str
    risk_factors: list[str]
    confidence: float
    iv_regime: str | None = None
    oi_structure: str | None = None


def parse_llm_response(raw: str) -> dict[str, Any]:
    """Parse and validate the LLM JSON response.

    The system prompt asks for a bare JSON object, but Claude occasionally
    wraps it in a ```json fence``` or prefixes a one-line preamble. We try
    plain ``json.loads`` first (the happy path), then fall back to extracting
    the largest ``{ … }`` substring before raising. This was the cause of
    ~18% silent Phase 3 failures on 2026-05-08.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Strip a single optional ```json fence``` wrapper, then a generic
        # ```fence```, then fall back to extracting the largest top-level {…}
        # object. We don't try to be clever about trailing text — if the
        # response has more than one object the first one wins.
        import re
        stripped = raw.strip()
        if stripped.startswith("```"):
            # Drop opening fence (```json or just ```), then closing fence
            stripped = re.sub(r"^```(?:json)?\s*\n?", "", stripped, count=1)
            stripped = re.sub(r"\n?```\s*$", "", stripped, count=1)
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            # Last-resort: regex out the first balanced-looking JSON object.
            # `re.DOTALL` lets `.` match newlines.
            m = re.search(r"\{.*\}", stripped, re.DOTALL)
            if not m:
                raise
            data = json.loads(m.group(0))

    decision = data.get("decision", "SKIP").upper()
    if decision not in ("PROCEED", "SKIP", "HEDGE"):
        decision = "SKIP"
    return {
        "decision": decision,
        "direction": data.get("direction", "neutral").lower(),
        "thesis": str(data.get("thesis", ""))[:500],
        "risk_factors": list(data.get("risk_factors", []))[:3],
        "confidence": float(data.get("confidence", 0.5)),
    }


def classify_oi_structure(pcr: float | None) -> str:
    """Derive a simple OI structure label from Put-Call Ratio."""
    if pcr is None:
        return "unknown"
    if pcr > 1.3:
        return "put_heavy"    # bullish support
    if pcr < 0.7:
        return "call_heavy"   # bearish resistance
    return "balanced"


def build_user_prompt(
    symbol: str,
    sector: str | None,
    underlying_price: float,
    iv_rank: float | None,
    iv_regime: str,
    oi_structure: str,
    days_to_expiry: int,
    news_score: float,
    sentiment_score: float,
    fii_dii_score: float | None,
    macro_align_score: float,
    convergence_score: float,
    composite_score: float,
    bullish_count: int,
    bearish_count: int,
    lookback_hours: int,
    fii_net_cr: float | None,
    dii_net_cr: float | None,
    macro_drivers: list[str],
    headlines: list[str],
    extra_context: str = "",
    market_movers_context: str = "",
) -> str:
    """Render the Phase 3 LLM user prompt.

    None-handling rules (so the LLM can distinguish "missing data" from
    "real-but-neutral data"):
      - ``iv_rank=None``       → "unknown (no IV history)" + iv_regime
                                  is overridden to "unknown" if not already
      - ``fii_net_cr=None`` OR ``dii_net_cr=None`` → "(data unavailable)"
                                  for the entire FII/DII line; the score is
                                  also rendered as "n/a" rather than 5.0.

    The prior version silently filled defaults (50.0% iv_rank, ₹0 FII/DII)
    that the LLM treated as real, disabling the system prompt's
    REGIME GATE and FII/DII alignment rules.
    """
    headlines_text = "\n".join(f"  - {h}" for h in headlines[:5]) or "  (no recent headlines)"

    if iv_rank is None:
        iv_rank_block = "unknown (no IV history)"
        # If caller passed iv_regime="neutral" but iv_rank was None, the
        # prompt would still claim a definitive "neutral regime" — force
        # consistency by labeling regime "unknown" when rank is unknown.
        if iv_regime not in ("unknown",):
            iv_regime = "unknown"
    else:
        iv_rank_block = f"{iv_rank:.1f}%"

    if fii_net_cr is None or dii_net_cr is None:
        fii_dii_block = "(data unavailable)"
    else:
        score_str = f"{fii_dii_score:.2f}" if fii_dii_score is not None else "n/a"
        fii_dii_block = (
            f"{score_str}/10 (FII net ₹{fii_net_cr:+.0f}Cr, "
            f"DII net ₹{dii_net_cr:+.0f}Cr)"
        )

    return FNO_THESIS_USER_TEMPLATE.format(
        symbol=symbol,
        sector=sector or "Unknown",
        underlying_price=f"{underlying_price:,.2f}",
        iv_rank_block=iv_rank_block,
        iv_regime=iv_regime,
        oi_structure=oi_structure,
        days_to_expiry=days_to_expiry,
        news_score=news_score,
        sentiment_score=sentiment_score,
        fii_dii_block=fii_dii_block,
        macro_align_score=macro_align_score,
        convergence_score=convergence_score,
        composite_score=composite_score,
        bullish_count=bullish_count,
        bearish_count=bearish_count,
        lookback_hours=lookback_hours,
        macro_drivers=", ".join(macro_drivers) or "N/A",
        headlines=headlines_text,
        market_movers_context=market_movers_context or "",
        extra_context=extra_context or "",
    )


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def _call_claude(prompt: str, model: str, temperature: float) -> tuple[str, int, int, int]:
    """Call Claude API synchronously. Returns (response_text, tokens_in, tokens_out, latency_ms)."""
    client = anthropic.Anthropic(api_key=_settings.anthropic_api_key)
    t0 = time.time()
    msg = client.messages.create(
        model=model,
        max_tokens=512,
        temperature=temperature,
        system=FNO_THESIS_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    latency_ms = int((time.time() - t0) * 1000)
    text = msg.content[0].text if msg.content else ""
    return text, msg.usage.input_tokens, msg.usage.output_tokens, latency_ms


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _get_phase2_candidates(session, run_date: date) -> list[FNOCandidate]:
    result = await session.execute(
        select(FNOCandidate).where(
            FNOCandidate.run_date == run_date,
            FNOCandidate.phase == 2,
        ).order_by(FNOCandidate.composite_score.desc())
        .limit(_settings.fno_phase3_target_output)
    )
    return list(result.scalars().all())


async def _get_instrument(session, instrument_id: str) -> Instrument | None:
    result = await session.execute(
        select(Instrument).where(Instrument.id == instrument_id)
    )
    return result.scalar_one_or_none()


async def _get_underlying_ltp(session, instrument_id: str) -> float | None:
    """Latest underlying spot price for an instrument.

    Tries the most recent OptionsChain.underlying_ltp first (intraday-fresh
    when chain collection ran today), then falls back to the latest non-null
    PriceDaily.close (yesterday's settle for off-hours runs). Returns None
    if neither source has data.

    Replaces the previous bug of passing ``instrument.market_cap_cr`` —
    that column is *market capitalization in crores*, not LTP, so the
    Phase 3 prompt was showing labels like "Underlying price: ₹100000"
    where the actual figure was the company's market cap. Trace at
    thesis_synthesizer.py:437 (pre-fix).
    """
    from src.models.fno_chain import OptionsChain
    from src.models.price import PriceDaily

    # Latest chain snapshot's underlying_ltp — usually the freshest source.
    # Filter `> 0` (not just isnot(None)) to skip rows with corrupt
    # underlying_ltp = 0 — those would otherwise short-circuit the
    # price_daily fallback and propagate ₹0.00 into the LLM prompt.
    chain_row = (await session.execute(
        select(OptionsChain.underlying_ltp)
        .where(
            OptionsChain.instrument_id == instrument_id,
            OptionsChain.underlying_ltp > 0,
        )
        .order_by(OptionsChain.snapshot_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    if chain_row is not None:
        return float(chain_row)

    # Fallback: most recent EOD close (also `> 0` for the same reason —
    # corporate-action / suspension rows can land as 0 in price_daily).
    pd_row = (await session.execute(
        select(PriceDaily.close)
        .where(
            PriceDaily.instrument_id == instrument_id,
            PriceDaily.close > 0,
        )
        .order_by(PriceDaily.date.desc())
        .limit(1)
    )).scalar_one_or_none()
    if pd_row is not None:
        return float(pd_row)

    return None


async def _get_iv_rank(session, instrument_id: str) -> tuple[float | None, float | None]:
    """Latest (iv_rank_52w, atm_iv) from iv_history for an instrument.

    Returns (None, None) if no row exists. The IV history builder runs
    EOD (15:40 IST) so on a normal market day this returns yesterday's
    settled values — appropriate for the pre-market Phase 3 prompt.

    Wires the IVHistory table that was always populated but never read
    by Phase 3 — the previous code did
    ``getattr(cand, "iv_rank_52w", None) or 50`` against an FNOCandidate
    that never carried the field, so iv_rank was permanently 50.
    """
    from src.models.fno_iv import IVHistory

    row = (await session.execute(
        select(IVHistory.iv_rank_52w, IVHistory.atm_iv)
        .where(IVHistory.instrument_id == instrument_id)
        .order_by(IVHistory.date.desc())
        .limit(1)
    )).first()
    if row is None:
        return None, None
    rank = float(row.iv_rank_52w) if row.iv_rank_52w is not None else None
    # Defensive: historical iv_history rows written before the
    # iv_history_builder clamp fix (2026-05-08) can have wildly out-of-range
    # values (-6273, +8100, etc.) caused by a unit mismatch in the
    # underlying chain iv column. Treat those as "no data" so the LLM sees
    # a neutral default rather than nonsense like "-587% IV rank".
    if rank is not None and not (0.0 <= rank <= 100.0):
        logger.warning(
            f"thesis_synthesizer: out-of-range iv_rank_52w={rank} for "
            f"instrument {instrument_id} — treating as missing. "
            f"Re-run iv_history_builder after the clamp fix to refresh."
        )
        rank = None
    atm = float(row.atm_iv) if row.atm_iv is not None else None
    return rank, atm


async def _get_chain_pcr(session, instrument_id: str) -> float | None:
    """Put-Call Ratio from the latest OptionsChain snapshot.

    PCR = ΣOI(PE) / ΣOI(CE) summed across all strikes for the latest
    snapshot's nearest expiry. Returns None when the chain has no rows
    or all OI is zero.

    Used by ``classify_oi_structure(pcr)`` to derive the ``oi_structure``
    label sent to the LLM — previously hardcoded to ``classify_oi_structure(None)``
    which always returned "unknown", silently disabling the system
    prompt's REGIME GATE rule.
    """
    from src.models.fno_chain import OptionsChain

    # Find the latest snapshot timestamp
    latest_snap = (await session.execute(
        select(func.max(OptionsChain.snapshot_at))
        .where(OptionsChain.instrument_id == instrument_id)
    )).scalar_one_or_none()
    if latest_snap is None:
        return None

    # Within that snapshot, pick the nearest expiry — far-month chains
    # add noise from buy-and-hold positions that don't reflect today's view
    nearest_expiry = (await session.execute(
        select(func.min(OptionsChain.expiry_date))
        .where(
            OptionsChain.instrument_id == instrument_id,
            OptionsChain.snapshot_at == latest_snap,
        )
    )).scalar_one_or_none()
    if nearest_expiry is None:
        return None

    # Sum OI for CE vs PE within (latest_snap, nearest_expiry)
    oi_rows = (await session.execute(
        select(OptionsChain.option_type, func.coalesce(func.sum(OptionsChain.oi), 0))
        .where(
            OptionsChain.instrument_id == instrument_id,
            OptionsChain.snapshot_at == latest_snap,
            OptionsChain.expiry_date == nearest_expiry,
        )
        .group_by(OptionsChain.option_type)
    )).all()
    ce_oi = pe_oi = 0
    for opt_type, total in oi_rows:
        if opt_type == "CE":
            ce_oi = int(total or 0)
        elif opt_type == "PE":
            pe_oi = int(total or 0)
    if ce_oi <= 0:
        return None
    return round(pe_oi / ce_oi, 4)


async def _get_news_counts(
    session,
    instrument_id: str,
    lookback_hours: int,
    *,
    anchor: datetime | None = None,
) -> tuple[int, int]:
    """Bullish / bearish Signal counts in the lookback window.

    Mirrors ``catalyst_scorer._get_news_counts`` (which is used by Phase
    2 for the news_score). Phase 3 needs the raw counts to surface in
    the prompt — previously hardcoded to ``bullish_count=0,
    bearish_count=0`` regardless of actual signal volume.

    ``anchor`` is the upper bound of the window (defaults to "now"). Pass
    the Phase-2 candidate's ``created_at`` so Phase 3's counts cover the
    same window Phase 2 used to compute ``news_score`` — otherwise the
    LLM can see ``news_score=10/10 (0 bullish, 0 bearish)`` when signals
    have aged past Phase 3's now-anchored window but not Phase 2's.
    """
    from src.models.signal import Signal

    upper = anchor if anchor is not None else datetime.now(tz=timezone.utc)
    cutoff = upper - timedelta(hours=lookback_hours)
    rows = (await session.execute(
        select(Signal.action, func.count(Signal.id))
        .where(
            Signal.instrument_id == instrument_id,
            Signal.created_at >= cutoff,
            Signal.created_at <= upper,
        )
        .group_by(Signal.action)
    )).all()
    bullish = sum(c for action, c in rows if action in ("BUY", "BULLISH"))
    bearish = sum(c for action, c in rows if action in ("SELL", "BEARISH"))
    return bullish, bearish


async def _get_headlines(session, instrument_id: str, lookback_hours: int) -> list[str]:
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=lookback_hours)
    result = await session.execute(
        select(Signal.reasoning)
        .where(
            Signal.instrument_id == instrument_id,
            Signal.created_at >= cutoff,
            Signal.reasoning.isnot(None),
        )
        .order_by(Signal.created_at.desc())
        .limit(5)
    )
    return [r for (r,) in result.all() if r]


# `_get_latest_fii_dii` previously lived here as a copy-paste of the
# catalyst_scorer helper. The duplication was caught during code review
# (2026-05-08) and consolidated — Phase 2 and Phase 3 must agree on the
# FII/DII contract, so a single canonical implementation lives there.
# Imported below at the top of the file via:
#     from src.fno.catalyst_scorer import get_latest_fii_dii


async def _write_audit_log(
    session,
    caller_ref_id: uuid.UUID,
    model: str,
    temperature: float,
    prompt: str,
    response: str,
    parsed: dict | None,
    tokens_in: int,
    tokens_out: int,
    latency_ms: int,
) -> None:
    session.add(LLMAuditLog(
        caller=_CALLER,
        caller_ref_id=caller_ref_id,
        model=model,
        temperature=temperature,
        prompt=prompt,
        response=response,
        response_parsed=parsed,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
    ))


async def _upsert_phase3_candidate(
    session,
    instrument_id: str,
    run_date: date,
    decision: str,
    thesis: str,
    technical_pass: bool,
    iv_regime: str | None,
    oi_structure: str | None,
    config_version: str,
) -> None:
    stmt = pg_insert(FNOCandidate).values(
        instrument_id=instrument_id,
        run_date=run_date,
        phase=3,
        llm_decision=decision,
        llm_thesis=thesis,
        technical_pass=technical_pass,
        iv_regime=iv_regime,
        oi_structure=oi_structure,
        config_version=config_version,
        created_at=datetime.now(tz=timezone.utc),
    ).on_conflict_do_update(
        index_elements=["instrument_id", "run_date", "phase"],
        set_={
            "llm_decision": decision,
            "llm_thesis": thesis,
            "technical_pass": technical_pass,
            "iv_regime": iv_regime,
            "oi_structure": oi_structure,
            "config_version": config_version,
        }
    )
    await session.execute(stmt)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_phase3(
    run_date: date | None = None,
    *,
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> list[ThesisResult]:
    """Run Phase 3 LLM thesis synthesis for top Phase-2 candidates.

    Returns list of ThesisResult. PROCEED/HEDGE candidates have a phase=3
    fno_candidates row written.

    ``as_of`` / ``dryrun_run_id`` follow the CLAUDE.md pipeline convention.
    ``as_of`` is currently used to pin the market-movers lookup window so a
    replay of a past day sees that day's prior-session bhavcopy rather than
    today's.
    """
    if run_date is None:
        run_date = date.today()

    # Defensive: when replaying a historical run_date the caller is expected
    # to also pass as_of, but if they forget we'd silently inject TODAY's
    # prior-session movers into a historical prompt — leaking future
    # context. Derive as_of from run_date in that case so the movers
    # lookup pins to the right day.
    if as_of is None and run_date != date.today():
        as_of = datetime(
            run_date.year, run_date.month, run_date.day,
            3, 30, tzinfo=timezone.utc,  # 09:00 IST — any time on run_date works
        )

    cfg = _settings
    model = cfg.fno_phase3_llm_model
    temperature = cfg.fno_phase3_llm_temperature
    lookback = cfg.fno_phase2_news_lookback_hours
    config_ver = cfg.fno_ranker_version

    async with session_scope() as session:
        candidates = await _get_phase2_candidates(session, run_date)
        fii_net, dii_net = await get_latest_fii_dii(session)

    if not candidates:
        logger.warning("fno.thesis: no Phase-2 candidates to synthesize")
        return []

    # Pull the prior-session movers once and reuse across all candidates —
    # the bhavcopy is a single archive file per day, so per-candidate calls
    # would be wasted disk reads. Failure here must not block Phase 3
    # (regime context is helpful, not required).
    movers = None
    try:
        from src.fno.market_movers import get_top_fno_movers
        movers = await get_top_fno_movers(
            top_n=10, bottom_n=5, as_of=as_of, dryrun_run_id=dryrun_run_id,
        )
    except Exception as exc:
        logger.warning(f"fno.thesis: market-movers fetch failed: {exc}")

    # Build the per-session enrichment block once — it's identical across
    # all candidates this run and pulling open_book/lessons/outcomes 12 times
    # would be wasteful. The portfolio_id below is the equity-strategy
    # portfolio (single F&O book in this codebase); when multi-portfolio F&O
    # lands, parametrise this. Failures degrade to empty so a missing table
    # never blocks Phase 3.
    extra_context = ""
    try:
        from sqlalchemy import text as _text

        from src.models.portfolio import Portfolio
        from src.trading.prompt_context import build_full_enrichment

        async with session_scope() as session:
            row = (await session.execute(
                _text(
                    "SELECT id FROM portfolios WHERE is_active "
                    "ORDER BY (name = 'Equity Strategy') DESC, created_at ASC "
                    "LIMIT 1"
                )
            )).first()
        eq_pid = row[0] if row else None
        if eq_pid is not None:
            extra_context = await build_full_enrichment(
                portfolio_id=eq_pid,
                asset_class="FNO",
                outcomes_window_days=10,
                lessons_lookback_days=60,
                lessons_limit=8,
            )
    except Exception as exc:
        logger.debug(f"fno.thesis: enrichment block skipped: {exc}")

    results: list[ThesisResult] = []

    for cand in candidates:
        inst_id = str(cand.instrument_id)
        ref_id = uuid.uuid4()
        try:
            async with session_scope() as session:
                instrument = await _get_instrument(session, inst_id)
                headlines = await _get_headlines(session, inst_id, lookback)
                # Real LTP — replaces the prior bug of passing market_cap_cr.
                # Tries OptionsChain.underlying_ltp first (intraday-fresh),
                # falls back to PriceDaily.close (yesterday's settle).
                underlying_ltp = await _get_underlying_ltp(session, inst_id)
                # Real IV rank from the iv_history table that the EOD
                # builder populates daily — wires data that was always
                # there but never queried.
                iv_rank_real, _atm_iv = await _get_iv_rank(session, inst_id)
                # Put-Call Ratio from latest chain snapshot, summed across
                # all strikes for the nearest expiry. None when chain has
                # no rows for this instrument.
                pcr = await _get_chain_pcr(session, inst_id)
                # Per-instrument bullish / bearish signal counts. Anchor
                # the lookback window to the Phase-2 cand row's created_at
                # so the counts cover the SAME window Phase 2 used when it
                # computed news_score — otherwise the LLM can see e.g.
                # `news_score=10/10 (0 bullish, 0 bearish)` (signals aged
                # past Phase 3's now-anchored window but not Phase 2's).
                bullish_count, bearish_count = await _get_news_counts(
                    session, inst_id, lookback, anchor=cand.created_at
                )

            if instrument is None:
                continue

            # Skip the candidate when LTP is unknown / corrupt rather than
            # send the LLM a "₹0.00" prompt. Phase-2 passers are expected
            # to have chain data, but a fresh-bootstrap symbol that
            # somehow squeaked through Phase 1 with one-sided OI but no
            # underlying_ltp would otherwise reach Phase 3 with garbage.
            if underlying_ltp is None or underlying_ltp <= 0:
                logger.warning(
                    f"fno.thesis: {instrument.symbol} skipped — "
                    f"no usable underlying LTP (chain + price_daily empty)"
                )
                continue

            # Pass real iv_rank through (may be None — build_user_prompt
            # will render "unknown (no IV history)" + force iv_regime to
            # 'unknown'). The previous `or 50.0` fallback masked missing
            # data as a real "neutral" value, silently disabling the
            # system prompt's REGIME GATE rule.
            if iv_rank_real is not None:
                iv_rank = iv_rank_real
                iv_regime = (
                    "high" if iv_rank > 70
                    else ("low" if iv_rank < 30 else "neutral")
                )
            else:
                iv_rank = None
                iv_regime = "unknown"

            # OI structure derived from real PCR (was hardcoded to "unknown")
            oi_structure = classify_oi_structure(pcr)

            from src.collectors.macro_collector import get_macro_drivers
            macro_drivers = get_macro_drivers(instrument.sector)

            days_to_expiry = (next_weekly_expiry(instrument.symbol, run_date) - run_date).days

            # Render the prior-session movers block, annotated for THIS
            # symbol if it appears in the leader/laggard lists.
            movers_block = ""
            if movers is not None:
                from src.fno.market_movers import render_movers_block
                movers_block = render_movers_block(
                    movers, instrument_symbol=instrument.symbol
                )

            # cand.fii_dii_score may be None (Phase 2 wrote None when
            # market FII/DII data was unavailable). Pass it through so
            # build_user_prompt can render "n/a" alongside the
            # "(data unavailable)" line — the previous `or 5` would have
            # silently rendered "5.0/10" indistinguishable from a real
            # neutral score.
            fii_dii_score = (
                float(cand.fii_dii_score)
                if cand.fii_dii_score is not None else None
            )

            prompt = build_user_prompt(
                symbol=instrument.symbol,
                sector=instrument.sector,
                underlying_price=underlying_ltp,
                iv_rank=iv_rank,
                iv_regime=iv_regime,
                oi_structure=oi_structure,
                days_to_expiry=days_to_expiry,
                news_score=float(cand.news_score or 5),
                sentiment_score=float(cand.sentiment_score or 5),
                fii_dii_score=fii_dii_score,
                macro_align_score=float(cand.macro_align_score or 5),
                convergence_score=float(cand.convergence_score or 5),
                composite_score=float(cand.composite_score or 5),
                bullish_count=bullish_count,
                bearish_count=bearish_count,
                lookback_hours=lookback,
                fii_net_cr=fii_net,
                dii_net_cr=dii_net,
                macro_drivers=macro_drivers,
                headlines=headlines,
                market_movers_context=movers_block,
                extra_context=extra_context,
            )

            raw_response, tokens_in, tokens_out, latency_ms = _call_claude(
                prompt, model, temperature
            )

            # Always persist the raw LLM call to the audit log first — even
            # if parsing fails we don't want to lose the forensic trail or
            # the token-cost record. A previous version called
            # parse_llm_response BEFORE _write_audit_log, which silently
            # discarded ~18% of Phase 3 calls on 2026-05-08.
            parsed: dict[str, Any] | None = None
            parse_error: str | None = None
            try:
                parsed = parse_llm_response(raw_response)
            except Exception as parse_exc:
                parse_error = f"{type(parse_exc).__name__}: {parse_exc}"
                logger.warning(
                    f"fno.thesis: {instrument.symbol} parse failed: {parse_error} "
                    f"(raw response saved to llm_audit_log)"
                )

            async with session_scope() as session:
                await _write_audit_log(
                    session, ref_id, model, temperature,
                    prompt, raw_response, parsed,
                    tokens_in, tokens_out, latency_ms,
                )
                if parsed is not None:
                    await _upsert_phase3_candidate(
                        session, inst_id, run_date,
                        parsed["decision"], parsed["thesis"],
                        parsed["decision"] == "PROCEED",
                        iv_regime, oi_structure, config_ver,
                    )

            if parsed is None:
                # Skip the candidate but DON'T mark it failed in the outer
                # try/except — the audit log already recorded what happened.
                continue

            result = ThesisResult(
                instrument_id=inst_id,
                symbol=instrument.symbol,
                decision=parsed["decision"],
                direction=parsed["direction"],
                thesis=parsed["thesis"],
                risk_factors=parsed["risk_factors"],
                confidence=parsed["confidence"],
                iv_regime=iv_regime,
                oi_structure=oi_structure,
            )
            results.append(result)
            logger.info(
                f"fno.thesis: {instrument.symbol} → {parsed['decision']} "
                f"(conf={parsed['confidence']:.2f})"
            )

        except Exception as exc:
            logger.warning(f"fno.thesis: instrument {inst_id} failed: {exc}")

    proceed_count = sum(1 for r in results if r.decision == "PROCEED")
    logger.info(f"fno.thesis: Phase 3 complete — {proceed_count}/{len(results)} PROCEED")
    return results
