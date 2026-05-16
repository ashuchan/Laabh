"""F&O Thesis Synthesizer — Phase 3 LLM-based trade thesis generation.

Runs after Phase 2 (catalyst scoring). For each Phase-2 passing instrument,
calls Claude to synthesize a trade thesis and records:
  - llm_decision: PROCEED | SKIP | HEDGE
  - llm_thesis: reasoning paragraph
  - iv_regime and oi_structure from chain data

Writes audit log entries via llm_audit_log for every API call.
"""
from __future__ import annotations

import asyncio
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
from src.fno.llm_features import LLMFeatureScore, parse_llm_features
from src.fno.prompts import (
    FNO_THESIS_PROMPT_VERSION,
    FNO_THESIS_PROMPT_VERSION_V10,
    FNO_THESIS_SYSTEM,
    FNO_THESIS_SYSTEM_V10,
    FNO_THESIS_USER_TEMPLATE,
)
from src.fno.vix_collector import classify_regime
from src.models.fno_candidate import FNOCandidate
from src.models.instrument import Instrument
from src.models.llm_audit_log import LLMAuditLog
from src.models.llm_decision_log import LLMDecisionLog
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
    vrp: float | None = None,
    vrp_regime: str | None = None,
    rv_20d: float | None = None,
    vol_surface_block: str = "(surface unavailable)",
    market_regime_block: str = "(regime unavailable)",
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

    # VRP block — shown when the EOD pipeline has populated rv_20d/vrp
    if vrp is not None and rv_20d is not None and vrp_regime is not None:
        vrp_pct = vrp * 100.0       # convert decimal to vol-points for readability
        rv_pct = rv_20d * 100.0
        vrp_block = (
            f"{vrp_regime.upper()} (VRP={vrp_pct:+.1f}vol pts: "
            f"ATM IV={iv_rank_block}, RV_20d={rv_pct:.1f}%)"
        )
    else:
        vrp_block = "(data unavailable — EOD VRP pipeline not yet run)"

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
        vrp_block=vrp_block,
        vol_surface_block=vol_surface_block,
        market_regime_block=market_regime_block,
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


def _call_claude_v10(prompt: str, model: str, temperature: float) -> tuple[str, int, int, int]:
    """v10 system-prompt variant. Mirrors ``_call_claude`` exactly except for
    the system block and a slightly higher max_tokens budget (the v10
    schema is wordier — strikes + reasoning_oneline).

    Prompt caching: the v10 system prompt is invariant across every call in
    a backfill run, so flagging it with ``cache_control: ephemeral`` lets
    Anthropic serve the cached copy and shaves ~30-50% of input-token cost
    on Sonnet (plan §3.2). Precedent at
    ``src/agents/runtime/workflow_runner.py:1117``.
    """
    client = anthropic.Anthropic(api_key=_settings.anthropic_api_key)
    t0 = time.time()
    msg = client.messages.create(
        model=model,
        max_tokens=600,
        temperature=temperature,
        system=[
            {
                "type": "text",
                "text": FNO_THESIS_SYSTEM_V10,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": prompt}],
    )
    latency_ms = int((time.time() - t0) * 1000)
    text = msg.content[0].text if msg.content else ""
    return text, msg.usage.input_tokens, msg.usage.output_tokens, latency_ms


async def _log_v10_shadow(
    *,
    prompt: str,
    model: str,
    run_date: date,
    as_of: datetime | None,
    dryrun_run_id: uuid.UUID | None,
    instrument_id: str,
    news_cutoff_at: datetime | None = None,
    instrument_tier: str | None = None,
    propensity_source: str = "live",
) -> None:
    """Fire-and-forget v10 call. Bounded timeout + single retry + exception
    swallow so a v10 failure never affects the v9 production path
    (plan §1.3 mandates the retry; review fix P1 #6).

    Run inside ``asyncio.create_task`` from the caller. The Anthropic SDK
    call is synchronous; wrap it with ``asyncio.to_thread`` so we don't
    block the event loop, then bound the whole thing with ``wait_for``.

    Optional audit-trail params (backfill plan §3.2 + §8):
      * ``news_cutoff_at`` — the latest ``published_at`` allowed in the
        prompt. NULL for live calls (no cutoff). Persisted as
        ``llm_decision_log.news_cutoff_at`` for the audit SQL.
      * ``instrument_tier`` — 'T1' / 'T2' snapshot so calibration can
        stratify fits without rejoining to fno_collection_tier.
      * ``propensity_source`` — 'live' (real bandit decision) | 'imputed'
        (backfill, 1/n_arms heuristic). Calibration applies a 0.3× weight
        multiplier to imputed rows.
    """
    raw: str | None = None
    tokens_in = tokens_out = latency_ms = 0
    # Up to 2 attempts (initial + 1 retry per plan §1.3). Retry only on
    # transient errors — timeout, network, SDK-level exception. Parse
    # failures don't retry (the model will return the same garbage).
    for attempt in range(2):
        try:
            loop_call = asyncio.to_thread(
                _call_claude_v10, prompt, model, _settings.fno_phase3_llm_temperature
            )
            raw, tokens_in, tokens_out, latency_ms = await asyncio.wait_for(
                loop_call, timeout=30.0
            )
            break
        except asyncio.TimeoutError:
            if attempt == 0:
                logger.debug(f"v10 shadow: {instrument_id} timed out — retrying once")
                continue
            logger.warning(f"v10 shadow: {instrument_id} timed out twice — skipping log")
            return
        except Exception as exc:
            if attempt == 0:
                logger.debug(f"v10 shadow: {instrument_id} retry after {exc!r}")
                continue
            logger.warning(f"v10 shadow: {instrument_id} failed after retry: {exc!r}")
            return

    if raw is None:
        return

    try:
        parsed = parse_llm_features(
            raw, as_of=as_of, dryrun_run_id=dryrun_run_id
        )
        # Mirror v9's audit-log write so the cost-per-trade comparator
        # has real v10 token counts to average against v9 (review fix
        # P0 #1). Same row also doubles as the forensic trail for any
        # v10 prompt regression debugging.
        v10_ref_id = uuid.uuid4()
        async with session_scope() as session:
            await _write_audit_log(
                session, v10_ref_id, model,
                _settings.fno_phase3_llm_temperature,
                prompt, raw,
                (parsed.raw if parsed is not None else None),
                tokens_in, tokens_out, latency_ms,
                caller=f"{_CALLER}.v10",
            )
            await _write_llm_decision_log(
                session,
                run_date=run_date,
                as_of=as_of or datetime.now(tz=timezone.utc),
                dryrun_run_id=dryrun_run_id,
                instrument_id=instrument_id,
                phase="fno_thesis",
                prompt_version=FNO_THESIS_PROMPT_VERSION_V10,
                model_id=model,
                decision_label=None,
                raw_response=(parsed.raw if parsed is not None else {"raw_text": raw}),
                directional_conviction=(parsed.directional_conviction if parsed else None),
                thesis_durability=(parsed.thesis_durability if parsed else None),
                catalyst_specificity=(parsed.catalyst_specificity if parsed else None),
                risk_flag=(parsed.risk_flag if parsed else None),
                raw_confidence=(parsed.raw_confidence if parsed else None),
                news_cutoff_at=news_cutoff_at,
                instrument_tier=instrument_tier,
                propensity_source=propensity_source,
            )
    except Exception as exc:
        # Swallow on purpose — shadow path must never crash the v9 caller.
        logger.warning(f"v10 shadow: {instrument_id} parse/log failed: {exc!r}")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _get_phase2_candidates(session, run_date: date) -> list[FNOCandidate]:
    # Order by conviction strength — furthest from neutral (5.0) first. This
    # handles both bullish days (composite > 5.5 ranked first) and bearish
    # days (composite < 4.5 ranked first), so the Phase 3 target-output cap
    # always picks the most actionable candidates regardless of market direction.
    result = await session.execute(
        select(FNOCandidate).where(
            FNOCandidate.run_date == run_date,
            FNOCandidate.phase == 2,
        ).order_by(func.abs(FNOCandidate.composite_score - 5.0).desc())
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


@dataclass(frozen=True)
class _IVSnapshot:
    """Typed container for IV + VRP data read from iv_history."""
    iv_rank_52w: float | None       # 0-100 percentile; None = no history
    atm_iv: float | None            # annualized decimal (e.g. 0.22 = 22%)
    rv_20d: float | None            # Yang-Zhang realized vol, annualized decimal
    vrp: float | None               # atm_iv - rv_20d; None = VRP not yet computed
    vrp_regime: str | None          # 'rich' | 'fair' | 'cheap' | None


async def _get_iv_snapshot(session, instrument_id: str) -> _IVSnapshot:
    """Latest IV rank, ATM IV, and VRP reading from iv_history.

    Returns an _IVSnapshot with None fields when data is unavailable.
    Callers should treat any None field as "missing" and render it
    explicitly in the LLM prompt (never substitute silent defaults).

    Replaces the retired _get_iv_rank() tuple return which leaked NoneType
    handling into every call site and added a new tuple position whenever
    the schema grew.
    """
    from src.models.fno_iv import IVHistory

    row = (await session.execute(
        select(
            IVHistory.iv_rank_52w,
            IVHistory.atm_iv,
            IVHistory.rv_20d,
            IVHistory.vrp,
            IVHistory.vrp_regime,
        )
        .where(
            IVHistory.instrument_id == instrument_id,
            IVHistory.dryrun_run_id.is_(None),
        )
        .order_by(IVHistory.date.desc())
        .limit(1)
    )).first()

    if row is None:
        return _IVSnapshot(None, None, None, None, None)

    # IV Rank validation — out-of-range values come from a historical unit
    # mismatch (atm_iv stored as decimal 0.27 while history was in % 33.36).
    # Clamp fix landed 2026-05-08; rows before that may carry garbage ranks.
    rank = float(row.iv_rank_52w) if row.iv_rank_52w is not None else None
    if rank is not None and not (0.0 <= rank <= 100.0):
        logger.warning(
            f"thesis_synthesizer: out-of-range iv_rank_52w={rank} for "
            f"instrument {instrument_id} — treating as missing."
        )
        rank = None

    # ATM IV normalization (same unit issue as iv_rank)
    atm_raw = float(row.atm_iv) if row.atm_iv is not None else None
    atm_dec = (atm_raw / 100.0 if (atm_raw and atm_raw > 3.0) else atm_raw)

    return _IVSnapshot(
        iv_rank_52w=rank,
        atm_iv=atm_dec,
        rv_20d=float(row.rv_20d) if row.rv_20d is not None else None,
        vrp=float(row.vrp) if row.vrp is not None else None,
        vrp_regime=row.vrp_regime,
    )


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


async def _get_headlines(
    session,
    instrument_id: str,
    lookback_hours: int,
    *,
    news_cutoff: datetime | None = None,
) -> list[str]:
    """Return the most recent ≤5 ``Signal.reasoning`` strings for the
    instrument, sanitised for prompt-injection.

    ``news_cutoff`` is the latest ``Signal.created_at`` allowed in the
    result; when None (live path), the upper bound is ``now()``. When set
    (historical replay), the window is ``[upper - lookback, upper]`` so
    signals created AFTER the replay timestamp can't leak future
    information into the prompt (backfill plan §3.2).

    Each row is run through ``news_sanitizer.sanitize_news_item`` so a
    poisoned RSS headline can't smuggle instructions into the LLM prompt
    (backfill plan §7.7).
    """
    from src.fno.news_sanitizer import sanitize_news_item

    upper = news_cutoff if news_cutoff is not None else datetime.now(tz=timezone.utc)
    cutoff = upper - timedelta(hours=lookback_hours)
    result = await session.execute(
        select(Signal.reasoning)
        .where(
            Signal.instrument_id == instrument_id,
            Signal.created_at >= cutoff,
            Signal.created_at <= upper,
            Signal.reasoning.isnot(None),
        )
        .order_by(Signal.created_at.desc())
        .limit(5)
    )
    raw_rows = [r for (r,) in result.all() if r]
    return [s for s in (sanitize_news_item(r) for r in raw_rows) if s]


# `_get_latest_fii_dii` previously lived here as a copy-paste of the
# catalyst_scorer helper. The duplication was caught during code review
# (2026-05-08) and consolidated — Phase 2 and Phase 3 must agree on the
# FII/DII contract, so a single canonical implementation lives there.
# Imported below at the top of the file via:
#     from src.fno.catalyst_scorer import get_latest_fii_dii


async def _write_llm_decision_log(
    session,
    *,
    run_date: date,
    as_of: datetime,
    dryrun_run_id: uuid.UUID | None,
    instrument_id: str,
    phase: str,
    prompt_version: str,
    model_id: str,
    decision_label: str | None,
    raw_response: dict,
    # v10 continuous fields (Phase 1+ — None when called from v9 path)
    directional_conviction: float | None = None,
    thesis_durability: float | None = None,
    catalyst_specificity: float | None = None,
    risk_flag: float | None = None,
    raw_confidence: float | None = None,
    # Backfill audit / calibration-hygiene fields (migration 0015)
    news_cutoff_at: datetime | None = None,
    instrument_tier: str | None = None,
    propensity_source: str | None = None,
    bandit_arm_propensity: float | None = None,
) -> None:
    """Insert one row into llm_decision_log for downstream calibration / outcome attribution.

    Uses ON CONFLICT DO NOTHING on the unique key so a retry within the same
    run_date / instrument / phase / prompt_version / dryrun_run_id tuple is a
    no-op rather than a crash — fast-track + LLM both call this, and we don't
    want duplicate writes when the same instrument is re-processed.

    ``news_cutoff_at`` / ``instrument_tier`` / ``propensity_source`` are
    populated by the backfill path (plan §3.2). Live call sites that don't
    pass them get DB defaults: ``propensity_source='unknown'``, others NULL.
    """
    values: dict = {
        "run_date": run_date,
        "as_of": as_of,
        "dryrun_run_id": dryrun_run_id,
        "instrument_id": instrument_id,
        "phase": phase,
        "prompt_version": prompt_version,
        "model_id": model_id,
        "decision_label": decision_label,
        "directional_conviction": directional_conviction,
        "thesis_durability": thesis_durability,
        "catalyst_specificity": catalyst_specificity,
        "risk_flag": risk_flag,
        "raw_confidence": raw_confidence,
        "raw_response": raw_response,
    }
    if news_cutoff_at is not None:
        values["news_cutoff_at"] = news_cutoff_at
    if instrument_tier is not None:
        values["instrument_tier"] = instrument_tier
    if propensity_source is not None:
        values["propensity_source"] = propensity_source
    if bandit_arm_propensity is not None:
        values["bandit_arm_propensity"] = bandit_arm_propensity

    stmt = pg_insert(LLMDecisionLog).values(**values).on_conflict_do_nothing(
        index_elements=["run_date", "instrument_id", "phase", "prompt_version", "dryrun_run_id"],
    )
    await session.execute(stmt)


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
    *,
    caller: str = _CALLER,
) -> None:
    """Write an llm_audit_log row. ``caller`` distinguishes v9 (default)
    from v10 shadow calls (``fno.thesis_synthesizer.v10``) so the
    cost-rollback comparator can compute a real ratio (review fix P0 #1).
    """
    session.add(LLMAuditLog(
        caller=caller,
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
    market_regime=None,  # RegimeResult | None — avoids circular import
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

    # News cutoff derived from as_of: when replaying a historical date we
    # must not let signals created after as_of leak into the prompt
    # (backfill plan §3.2). When as_of is None (live), no cutoff is
    # applied — the existing "last lookback_hours" window stands.
    news_cutoff = as_of  # None in live mode → callees default to now()
    # Tier-snapshot module imported here to avoid a top-level cycle.
    from src.fno.tier_manager import get_tier_label

    for cand in candidates:
        inst_id = str(cand.instrument_id)
        ref_id = uuid.uuid4()
        try:
            async with session_scope() as session:
                instrument = await _get_instrument(session, inst_id)
                headlines = await _get_headlines(
                    session, inst_id, lookback, news_cutoff=news_cutoff
                )
                underlying_ltp = await _get_underlying_ltp(session, inst_id)
                iv_snap = await _get_iv_snapshot(session, inst_id)
                pcr = await _get_chain_pcr(session, inst_id)
                # When backfilling, anchor the news-count window on as_of
                # rather than cand.created_at — cand.created_at reflects
                # when Phase 2 RAN, which for a historical replay is
                # actually today (the replay moment), not the historical
                # date being replayed. as_of carries the correct anchor.
                bullish_count, bearish_count = await _get_news_counts(
                    session, inst_id, lookback,
                    anchor=as_of if as_of is not None else cand.created_at,
                )
                tier_label = await get_tier_label(inst_id, session=session)

            # Vol surface read is outside the session block — it opens its own session.
            from src.fno.vol_surface import get_latest_surface
            vol_surface = await get_latest_surface(inst_id, run_date)

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

            # IV rank and regime — derived from typed snapshot
            if iv_snap.iv_rank_52w is not None:
                iv_rank = iv_snap.iv_rank_52w
                iv_regime = (
                    "high" if iv_rank > 70
                    else ("low" if iv_rank < 30 else "neutral")
                )
            else:
                iv_rank = None
                iv_regime = "unknown"

            # OI structure derived from real PCR
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

            vol_surface_block = (
                vol_surface.as_prompt_block()
                if vol_surface is not None
                else "(surface unavailable — pre-market computation not yet run)"
            )

            market_regime_block = (
                market_regime.as_prompt_block()
                if market_regime is not None
                else "(regime unavailable)"
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
                vrp=iv_snap.vrp,
                vrp_regime=iv_snap.vrp_regime,
                rv_20d=iv_snap.rv_20d,
                vol_surface_block=vol_surface_block,
                market_regime_block=market_regime_block,
                market_movers_context=movers_block,
                extra_context=extra_context,
            )

            # -------------------------------------------------------------------
            # Fast-track: deterministic PROCEED for extreme-conviction candidates
            # in a confirmed trend with cheap VRP.
            #
            # Rationale: when |composite - 5.0| >= threshold AND regime is a
            # confirmed directional trend AND VRP is cheap (realized vol > IV),
            # debit spreads have a structural edge regardless of stock-specific
            # narrative. Sending these to the LLM adds cost and latency while
            # the outcome is nearly certain — the LLM was SKIPping them due to
            # the THESIS DURABILITY rule misapplied to multi-session trends.
            #
            # Fast-track writes a synthetic PROCEED to fno_candidates and the
            # audit log (model="fast_track_v1"), then `continue`s to skip the
            # LLM call. The ML shadow still runs for data collection.
            # -------------------------------------------------------------------
            _fast_tracked = False
            _composite_val = float(cand.composite_score or 5)
            _deviation = abs(_composite_val - 5.0)
            _ft_threshold = cfg.fno_phase3_fast_track_threshold

            if (
                _deviation >= _ft_threshold
                and market_regime is not None
                and market_regime.regime in ("trending_bear", "trending_bull")
                and iv_snap.vrp_regime == "cheap"
                and days_to_expiry >= 3
            ):
                _direction = "bearish" if _composite_val < 5 else "bullish"
                _structure = "bear_put_spread" if _direction == "bearish" else "bull_call_spread"
                _vrp_pts = (iv_snap.vrp or 0) * 100
                _ft_confidence = round(min(0.85, 0.55 + (_deviation - _ft_threshold) * 0.15), 3)

                _ft_parsed: dict[str, Any] = {
                    "decision": "PROCEED",
                    "direction": _direction,
                    "thesis": (
                        f"Fast-track PROCEED: extreme {_direction} conviction "
                        f"(composite={_composite_val:.2f}, deviation={_deviation:.2f} from neutral). "
                        f"Regime={market_regime.regime} confirmed over multiple sessions. "
                        f"VRP={_vrp_pts:+.1f}vpts (cheap) gives debit buyer structural edge — "
                        f"realized moves exceed premium paid. Structure: {_structure}."
                    ),
                    "risk_factors": [
                        "trend_reversal_before_expiry",
                        "theta_decay_if_trend_stalls",
                    ],
                    "confidence": _ft_confidence,
                }

                import json as _json
                _ft_raw = _json.dumps(_ft_parsed)

                async with session_scope() as session:
                    await _write_audit_log(
                        session, ref_id, "fast_track_v1", 0.0,
                        f"[FAST-TRACK] {instrument.symbol} comp={_composite_val:.2f} "
                        f"dev={_deviation:.2f} regime={market_regime.regime} "
                        f"vrp={_vrp_pts:+.1f}vpts",
                        _ft_raw, _ft_parsed,
                        0, 0, 0,
                    )
                    await _upsert_phase3_candidate(
                        session, inst_id, run_date,
                        _ft_parsed["decision"], _ft_parsed["thesis"],
                        True,
                        iv_regime, oi_structure, f"{config_ver}_ft",
                    )
                    # Shadow row for the LLM-feature-generator data layer.
                    # model_id is the synthetic 'fast_track_v1' so calibration
                    # can stratify or exclude these — they bypass the LLM.
                    await _write_llm_decision_log(
                        session,
                        run_date=run_date,
                        as_of=as_of or datetime.now(tz=timezone.utc),
                        dryrun_run_id=dryrun_run_id,
                        instrument_id=inst_id,
                        phase="fno_thesis",
                        prompt_version=f"{FNO_THESIS_PROMPT_VERSION}_ft",
                        model_id="fast_track_v1",
                        decision_label=_ft_parsed["decision"],
                        raw_response=_ft_parsed,
                        raw_confidence=_ft_confidence,
                    )

                result = ThesisResult(
                    instrument_id=inst_id,
                    symbol=instrument.symbol,
                    decision="PROCEED",
                    direction=_direction,
                    thesis=_ft_parsed["thesis"],
                    risk_factors=_ft_parsed["risk_factors"],
                    confidence=_ft_confidence,
                    iv_regime=iv_regime,
                    oi_structure=oi_structure,
                )
                results.append(result)
                logger.info(
                    f"fno.thesis: {instrument.symbol} FAST-TRACK -> PROCEED "
                    f"({_structure}, conf={_ft_confidence:.2f}, "
                    f"dev={_deviation:.2f}, vrp={_vrp_pts:+.1f}vpts)"
                )
                _fast_tracked = True

            # ML shadow prediction — runs before the LLM call to avoid
            # anchoring bias. The prediction is stored and later compared
            # to the LLM decision for agreement tracking.
            _ml_pred = _ml_conf = _ml_row_id = None
            try:
                from src.fno.ml_decision import extract_features, record_prediction
                _ml_features = extract_features(
                    candidate_id=str(cand.id),
                    composite=float(cand.composite_score or 5),
                    news=float(cand.news_score or 5),
                    sentiment=float(cand.sentiment_score or 5),
                    fii_dii=fii_dii_score,
                    macro=float(cand.macro_align_score or 5),
                    convergence=float(cand.convergence_score or 5),
                    iv_rank=iv_snap.iv_rank_52w,
                    vrp=iv_snap.vrp,
                    vrp_regime=iv_snap.vrp_regime,
                    skew_regime=vol_surface.skew_regime if vol_surface else None,
                    term_regime=vol_surface.term_regime if vol_surface else None,
                    pcr=vol_surface.pcr_near_expiry if vol_surface else None,
                    vix=market_regime.vix_current if market_regime else None,
                    market_regime=market_regime.regime if market_regime else None,
                    days_to_expiry=days_to_expiry,
                    atm_iv=iv_snap.atm_iv,
                )
                _ml_pred, _ml_conf, _ml_row_id = await record_prediction(
                    str(cand.id), inst_id, run_date, _ml_features
                )
                logger.debug(
                    f"fno.thesis: {instrument.symbol} ML={_ml_pred} ({_ml_conf:.2f}) "
                    f"[shadow, LLM not yet called]"
                )
            except Exception as ml_exc:
                logger.debug(f"fno.thesis: ML shadow failed: {ml_exc!r}")

            if _fast_tracked:
                # Even fast-track names get a v10 shadow score — calibration
                # needs to see the model's view on extreme-conviction setups
                # as well as borderline ones. Always log v10 when enabled,
                # independent of laabh_llm_mode (which only governs whether
                # v10 features drive the bandit).
                if _settings.laabh_llm_v10_logging_enabled:
                    asyncio.create_task(_log_v10_shadow(
                        prompt=prompt,
                        model=model,
                        run_date=run_date,
                        as_of=as_of,
                        dryrun_run_id=dryrun_run_id,
                        instrument_id=inst_id,
                        news_cutoff_at=news_cutoff,
                        instrument_tier=tier_label,
                        # 'live' for the live caller (no dryrun_run_id);
                        # backfill drives v10 through a different path
                        # (scripts/backfill_llm_features.py) that sets
                        # propensity_source='imputed' explicitly.
                        propensity_source="live" if dryrun_run_id is None else "imputed",
                    ))
                continue   # audit log + candidate already written above

            raw_response, tokens_in, tokens_out, latency_ms = _call_claude(
                prompt, model, temperature
            )

            # Fire-and-forget v10 shadow call. Runs in parallel with the v9
            # write block below; bounded timeout (30s) and exception
            # swallow inside the helper so a v10 failure cannot affect the
            # v9 production path. Always fires when v10 logging is on —
            # laabh_llm_mode controls bandit semantics, not whether v10 is
            # logged for calibration.
            if _settings.laabh_llm_v10_logging_enabled:
                asyncio.create_task(_log_v10_shadow(
                    prompt=prompt,
                    model=model,
                    run_date=run_date,
                    as_of=as_of,
                    dryrun_run_id=dryrun_run_id,
                    instrument_id=inst_id,
                    news_cutoff_at=news_cutoff,
                    instrument_tier=tier_label,
                    propensity_source="live" if dryrun_run_id is None else "imputed",
                ))

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

            # Update shadow prediction with actual LLM decision
            if parsed is not None and _ml_pred is not None:
                try:
                    from src.fno.ml_decision import update_llm_outcome
                    await update_llm_outcome(
                        str(cand.id), parsed["decision"], parsed.get("confidence", 0.5)
                    )
                    agreed = _ml_pred == parsed["decision"]
                    logger.debug(
                        f"fno.thesis: {instrument.symbol} ML={_ml_pred} "
                        f"LLM={parsed['decision']} agreed={agreed}"
                    )
                except Exception:
                    pass

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
                # Shadow row for the LLM-feature-generator data layer.
                # When the parse failed (parsed is None) we still record the
                # raw text so calibration / audit can see the failure rate.
                _raw_for_log: dict[str, Any] = (
                    parsed if parsed is not None else {"raw_text": raw_response, "parse_error": parse_error}
                )
                await _write_llm_decision_log(
                    session,
                    run_date=run_date,
                    as_of=as_of or datetime.now(tz=timezone.utc),
                    dryrun_run_id=dryrun_run_id,
                    instrument_id=inst_id,
                    phase="fno_thesis",
                    prompt_version=FNO_THESIS_PROMPT_VERSION,
                    model_id=model,
                    decision_label=(parsed["decision"] if parsed is not None else None),
                    raw_response=_raw_for_log,
                    raw_confidence=(parsed.get("confidence") if parsed is not None else None),
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


# ---------------------------------------------------------------------------
# v10 backfill entry point (plan §3.2 Phase B)
# ---------------------------------------------------------------------------


async def run_v10_backfill_one_candidate(
    *,
    candidate_id: str,
    run_date: date,
    as_of: datetime,
    dryrun_run_id: uuid.UUID,
    news_cutoff: datetime,
    bandit_arm_propensity: float | None,
    propensity_source: str = "imputed",
    market_regime=None,
) -> dict:
    """Backfill one v10 row into ``llm_decision_log`` for a single Phase-2
    candidate on a historical run_date.

    Differs from :func:`run_phase3` in three ways:
      1. **v10 only** — does NOT call v9 (``_call_claude``) and does NOT
         upsert into ``fno_candidates`` phase=3. The backfill is purely
         for calibration data; we do not want to pollute the live
         decisioning surface with replayed historical decisions.
      2. **Awaited, not fire-and-forget** — the caller can rate-limit and
         track cumulative cost across candidates. Returns
         ``{'tokens_in', 'tokens_out', 'latency_ms', 'wrote_row'}``.
      3. **Idempotent skip** — if an llm_decision_log row already exists
         for ``(run_date, instrument_id, 'fno_thesis', 'v10_continuous',
         dryrun_run_id)`` the function returns immediately without an
         LLM call. This is the per-candidate resume semantics the plan
         calls for in §3.2.

    Plan reference: backfill_plan.md §3.2 Phase B.
    """
    cfg = _settings
    model = cfg.fno_phase3_llm_model
    temperature = cfg.fno_phase3_llm_temperature
    lookback = cfg.fno_phase2_news_lookback_hours

    # Single session covers: resume check + Phase 2 candidate lookup +
    # all prompt-context fetches. Reducing the round-trip count from
    # three sessions to one is the main optimisation for the 1500-row
    # backfill. Vol surface uses its own session intentionally (it does
    # internal session_scope management — see get_latest_surface).
    from src.fno.tier_manager import get_tier_label

    async with session_scope() as session:
        existing_row = (await session.execute(
            select(LLMDecisionLog.id).where(
                LLMDecisionLog.run_date == run_date,
                LLMDecisionLog.instrument_id == candidate_id,
                LLMDecisionLog.phase == "fno_thesis",
                LLMDecisionLog.prompt_version == FNO_THESIS_PROMPT_VERSION_V10,
                LLMDecisionLog.dryrun_run_id == dryrun_run_id,
            )
        )).scalar_one_or_none()
        if existing_row is not None:
            return {"tokens_in": 0, "tokens_out": 0, "latency_ms": 0, "wrote_row": False}

        cand = (await session.execute(
            select(FNOCandidate).where(
                FNOCandidate.run_date == run_date,
                FNOCandidate.instrument_id == candidate_id,
                FNOCandidate.phase == 2,
            )
        )).scalar_one_or_none()
        if cand is None:
            return {"tokens_in": 0, "tokens_out": 0, "latency_ms": 0, "wrote_row": False}

        instrument = await _get_instrument(session, candidate_id)
        if instrument is None:
            return {"tokens_in": 0, "tokens_out": 0, "latency_ms": 0, "wrote_row": False}

        headlines = await _get_headlines(
            session, candidate_id, lookback, news_cutoff=news_cutoff
        )
        underlying_ltp = await _get_underlying_ltp(session, candidate_id)
        iv_snap = await _get_iv_snapshot(session, candidate_id)
        pcr = await _get_chain_pcr(session, candidate_id)
        bullish_count, bearish_count = await _get_news_counts(
            session, candidate_id, lookback, anchor=as_of,
        )
        fii_net, dii_net = await get_latest_fii_dii(session)
        tier_label = await get_tier_label(candidate_id, session=session)

    if underlying_ltp is None or underlying_ltp <= 0:
        logger.warning(
            f"v10 backfill: {instrument.symbol} {run_date} skipped — "
            "no usable underlying LTP at historical cutoff"
        )
        return {"tokens_in": 0, "tokens_out": 0, "latency_ms": 0, "wrote_row": False}

    iv_rank = iv_snap.iv_rank_52w
    if iv_rank is not None:
        iv_regime = "high" if iv_rank > 70 else ("low" if iv_rank < 30 else "neutral")
    else:
        iv_regime = "unknown"
    oi_structure = classify_oi_structure(pcr)
    days_to_expiry = (next_weekly_expiry(instrument.symbol, run_date) - run_date).days
    from src.collectors.macro_collector import get_macro_drivers
    macro_drivers = get_macro_drivers(instrument.sector)

    # Vol surface — same call run_phase3 uses; opens its own session.
    from src.fno.vol_surface import get_latest_surface
    vol_surface = await get_latest_surface(candidate_id, run_date)
    vol_surface_block = (
        vol_surface.as_prompt_block()
        if vol_surface is not None
        else "(surface unavailable — pre-market computation not yet run)"
    )

    market_regime_block = (
        market_regime.as_prompt_block() if market_regime is not None
        else "(regime unavailable)"
    )

    fii_dii_score = (
        float(cand.fii_dii_score) if cand.fii_dii_score is not None else None
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
        vrp=iv_snap.vrp,
        vrp_regime=iv_snap.vrp_regime,
        rv_20d=iv_snap.rv_20d,
        vol_surface_block=vol_surface_block,
        market_regime_block=market_regime_block,
    )

    # Call Claude. Single retry on transient failure.
    raw_response: str | None = None
    tokens_in = tokens_out = latency_ms = 0
    for attempt in range(2):
        try:
            raw_response, tokens_in, tokens_out, latency_ms = await asyncio.to_thread(
                _call_claude_v10, prompt, model, temperature
            )
            break
        except Exception as exc:
            if attempt == 0:
                logger.debug(f"v10 backfill: {instrument.symbol} retry after {exc!r}")
                continue
            raise

    if raw_response is None:
        raise RuntimeError("v10 backfill: Claude returned no response after retries")

    parsed = parse_llm_features(raw_response)

    ref_id = uuid.uuid4()
    async with session_scope() as session:
        await _write_audit_log(
            session, ref_id, model, temperature,
            prompt, raw_response,
            (parsed.raw if parsed is not None else None),
            tokens_in, tokens_out, latency_ms,
            caller=f"{_CALLER}.v10.backfill",
        )
        await _write_llm_decision_log(
            session,
            run_date=run_date,
            as_of=as_of,
            dryrun_run_id=dryrun_run_id,
            instrument_id=candidate_id,
            phase="fno_thesis",
            prompt_version=FNO_THESIS_PROMPT_VERSION_V10,
            model_id=model,
            decision_label=None,
            raw_response=(parsed.raw if parsed is not None else {"raw_text": raw_response}),
            directional_conviction=(parsed.directional_conviction if parsed else None),
            thesis_durability=(parsed.thesis_durability if parsed else None),
            catalyst_specificity=(parsed.catalyst_specificity if parsed else None),
            risk_flag=(parsed.risk_flag if parsed else None),
            raw_confidence=(parsed.raw_confidence if parsed else None),
            news_cutoff_at=news_cutoff,
            instrument_tier=tier_label,
            propensity_source=propensity_source,
            bandit_arm_propensity=bandit_arm_propensity,
        )

    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "latency_ms": latency_ms,
        "wrote_row": True,
    }
