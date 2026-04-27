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
from src.fno.prompts import (
    FNO_THESIS_PROMPT_VERSION,
    FNO_THESIS_SYSTEM,
    FNO_THESIS_USER_TEMPLATE,
)
from src.fno.vix_collector import classify_regime
from src.models.content import RawContent
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
    """Parse and validate the LLM JSON response."""
    data = json.loads(raw)
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
    fii_dii_score: float,
    macro_align_score: float,
    convergence_score: float,
    composite_score: float,
    bullish_count: int,
    bearish_count: int,
    lookback_hours: int,
    fii_net_cr: float,
    dii_net_cr: float,
    macro_drivers: list[str],
    headlines: list[str],
) -> str:
    headlines_text = "\n".join(f"  - {h}" for h in headlines[:5]) or "  (no recent headlines)"
    return FNO_THESIS_USER_TEMPLATE.format(
        symbol=symbol,
        sector=sector or "Unknown",
        underlying_price=f"{underlying_price:,.2f}",
        iv_rank=f"{iv_rank:.1f}" if iv_rank is not None else "N/A",
        iv_regime=iv_regime,
        oi_structure=oi_structure,
        days_to_expiry=days_to_expiry,
        news_score=news_score,
        sentiment_score=sentiment_score,
        fii_dii_score=fii_dii_score,
        macro_align_score=macro_align_score,
        convergence_score=convergence_score,
        composite_score=composite_score,
        bullish_count=bullish_count,
        bearish_count=bearish_count,
        lookback_hours=lookback_hours,
        fii_net_cr=f"{fii_net_cr:+.0f}",
        dii_net_cr=f"{dii_net_cr:+.0f}",
        macro_drivers=", ".join(macro_drivers) or "N/A",
        headlines=headlines_text,
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


async def _get_headlines(session, instrument_id: str, lookback_hours: int) -> list[str]:
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=lookback_hours)
    result = await session.execute(
        select(Signal.summary)
        .where(
            Signal.instrument_id == instrument_id,
            Signal.created_at >= cutoff,
            Signal.summary.isnot(None),
        )
        .order_by(Signal.created_at.desc())
        .limit(5)
    )
    return [r for (r,) in result.all() if r]


async def _get_latest_fii_dii(session) -> tuple[float, float]:
    result = await session.execute(
        select(RawContent.content_text)
        .where(RawContent.media_type == "fii_dii")
        .order_by(RawContent.fetched_at.desc())
        .limit(1)
    )
    raw = result.scalar_one_or_none()
    if not raw:
        return 0.0, 0.0
    try:
        data = json.loads(raw)
        return float(data.get("fii_net_cr", 0.0)), float(data.get("dii_net_cr", 0.0))
    except Exception:
        return 0.0, 0.0


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

async def run_phase3(run_date: date | None = None) -> list[ThesisResult]:
    """Run Phase 3 LLM thesis synthesis for top Phase-2 candidates.

    Returns list of ThesisResult. PROCEED/HEDGE candidates have a phase=3
    fno_candidates row written.
    """
    if run_date is None:
        run_date = date.today()

    cfg = _settings
    model = cfg.fno_phase3_llm_model
    temperature = cfg.fno_phase3_llm_temperature
    lookback = cfg.fno_phase2_news_lookback_hours
    config_ver = cfg.fno_ranker_version

    async with session_scope() as session:
        candidates = await _get_phase2_candidates(session, run_date)
        fii_net, dii_net = await _get_latest_fii_dii(session)

    if not candidates:
        logger.warning("fno.thesis: no Phase-2 candidates to synthesize")
        return []

    results: list[ThesisResult] = []

    for cand in candidates:
        inst_id = str(cand.instrument_id)
        ref_id = uuid.uuid4()
        try:
            async with session_scope() as session:
                instrument = await _get_instrument(session, inst_id)
                headlines = await _get_headlines(session, inst_id, lookback)

            if instrument is None:
                continue

            # Derive iv_regime from iv_rank (simplified — no live VIX needed)
            iv_rank = float(cand.iv_rank_52w or 50)
            iv_regime = "high" if iv_rank > 70 else ("low" if iv_rank < 30 else "neutral")

            # OI structure from chain PCR (stubbed to unknown if no data)
            oi_structure = classify_oi_structure(None)

            from src.collectors.macro_collector import get_macro_drivers
            macro_drivers = get_macro_drivers(instrument.sector)

            days_to_expiry = (next_weekly_expiry(instrument.symbol, run_date) - run_date).days

            prompt = build_user_prompt(
                symbol=instrument.symbol,
                sector=instrument.sector,
                underlying_price=float(instrument.market_cap_cr or 0),
                iv_rank=float(cand.iv_rank_52w or 50),
                iv_regime=iv_regime,
                oi_structure=oi_structure,
                days_to_expiry=days_to_expiry,
                news_score=float(cand.news_score or 5),
                sentiment_score=float(cand.sentiment_score or 5),
                fii_dii_score=float(cand.fii_dii_score or 5),
                macro_align_score=float(cand.macro_align_score or 5),
                convergence_score=float(cand.convergence_score or 5),
                composite_score=float(cand.composite_score or 5),
                bullish_count=0,
                bearish_count=0,
                lookback_hours=lookback,
                fii_net_cr=fii_net,
                dii_net_cr=dii_net,
                macro_drivers=macro_drivers,
                headlines=headlines,
            )

            raw_response, tokens_in, tokens_out, latency_ms = _call_claude(
                prompt, model, temperature
            )
            parsed = parse_llm_response(raw_response)

            async with session_scope() as session:
                await _write_audit_log(
                    session, ref_id, model, temperature,
                    prompt, raw_response, parsed,
                    tokens_in, tokens_out, latency_ms,
                )
                await _upsert_phase3_candidate(
                    session, inst_id, run_date,
                    parsed["decision"], parsed["thesis"],
                    parsed["decision"] == "PROCEED",
                    iv_regime, oi_structure, config_ver,
                )

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
