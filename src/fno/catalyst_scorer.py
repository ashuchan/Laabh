"""F&O Catalyst Scorer — Phase 2 signal scoring pipeline.

Runs after Phase 1 (liquidity filter) selects the candidate universe.
For each Phase-1 passing instrument, Phase 2 scores five catalyst dimensions:

  1. news_score       (0-10): bullish/bearish signal count from recent news
  2. sentiment_score  (0-10): from market_sentiment table (India VIX, PCR, etc.)
  3. fii_dii_score    (0-10): derived from FII/DII net buy/sell data
  4. macro_align_score(0-10): macro instruments trending in the right direction
  5. convergence_score(0-10): how many dimensions agree on direction

composite_score = weighted sum of the five dimensions.

Only instruments with composite_score ≥ config.fno_phase2_min_composite_score
proceed to Phase 3 (thesis synthesis).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Sequence

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.config import Settings
from src.collectors.macro_collector import get_macro_direction, get_macro_drivers
from src.db import session_scope
from src.models.content import RawContent
from src.models.fno_candidate import FNOCandidate
from src.models.instrument import Instrument

_settings = Settings()


# ---------------------------------------------------------------------------
# Pure scoring helpers (no I/O — fully unit-testable)
# ---------------------------------------------------------------------------

def score_news(bullish_count: int, bearish_count: int) -> float:
    """Score 0-10 based on net bullish news signals.

    Net positive → score above 5; net negative → below 5; neutral = 5.
    """
    net = bullish_count - bearish_count
    total = bullish_count + bearish_count
    if total == 0:
        return 5.0
    ratio = net / total  # -1 to +1
    return round(5.0 + ratio * 5.0, 2)


def score_fii_dii(fii_net_cr: float, dii_net_cr: float) -> float:
    """Score 0-10 based on FII + DII net activity.

    Both buying → 8-10; both selling → 0-2; mixed → 4-6.
    Each contributes equally; net >= +500 Cr is considered strongly bullish.
    """
    fii_score = _net_cr_to_score(fii_net_cr, threshold=500.0)
    dii_score = _net_cr_to_score(dii_net_cr, threshold=300.0)
    return round((fii_score + dii_score) / 2, 2)


def _net_cr_to_score(net_cr: float, threshold: float) -> float:
    """Map net crore value to 0-10 score. threshold = strong bullish level."""
    if net_cr >= threshold:
        return 10.0
    if net_cr <= -threshold:
        return 0.0
    # Linear interpolation between -threshold and +threshold
    return round(5.0 + (net_cr / threshold) * 5.0, 2)


def score_fii_dii_for_instrument(
    market_fii_net_cr: float,
    market_dii_net_cr: float,
    stock_pct_change: float | None,
    *,
    alignment_bonus: float = 1.5,
    pct_threshold: float = 0.5,
) -> float:
    """Per-instrument FII/DII score.

    NSE doesn't publish per-stock FII flows on a daily cadence we can
    cheaply consume, so this is an alignment proxy: take the market-wide
    FII/DII signal and modulate it by *this* stock's recent price action.

    Why: a market-wide bullish FII print is far more informative for a
    stock that's *also* up than for one that's diverging — institutional
    buying tends to concentrate in winners. Conversely a market-wide
    bullish print where the stock is *down* is a divergence signal worth
    discounting.

    Rules:
      - market_score > 5.5 (bullish) AND stock_pct >= +pct_threshold → +bonus
      - market_score < 4.5 (bearish) AND stock_pct <= -pct_threshold → +bonus (alignment with selling)
      - market_score > 5.5 AND stock_pct <= -pct_threshold → -bonus (divergence)
      - market_score < 4.5 AND stock_pct >= +pct_threshold → -bonus (divergence)
      - otherwise (neutral market or stock) → unchanged

    Falls back to the unmodulated market score when ``stock_pct_change`` is
    None (no recent price data for the instrument).
    """
    market_score = score_fii_dii(market_fii_net_cr, market_dii_net_cr)
    if stock_pct_change is None:
        return market_score

    market_dir = 1 if market_score > 5.5 else (-1 if market_score < 4.5 else 0)
    if market_dir == 0:
        return market_score  # market signal too weak to modulate

    stock_dir = (
        1 if stock_pct_change >= pct_threshold
        else (-1 if stock_pct_change <= -pct_threshold else 0)
    )
    if stock_dir == 0:
        return market_score  # stock barely moved; no alignment info

    if market_dir == stock_dir:
        return round(min(10.0, market_score + alignment_bonus), 2)
    return round(max(0.0, market_score - alignment_bonus), 2)


def score_macro(
    sector: str | None,
    macro_snapshots: dict[str, float],
) -> float:
    """Score 0-10 based on how many relevant macro instruments are bullish.

    macro_snapshots: {macro_name: change_pct}
    """
    drivers = get_macro_drivers(sector)
    if not drivers:
        return 5.0

    scores = []
    for driver in drivers:
        change_pct = macro_snapshots.get(driver)
        if change_pct is None:
            continue
        direction = get_macro_direction(driver, change_pct)
        if direction == "bullish":
            scores.append(10.0)
        elif direction == "bearish":
            scores.append(0.0)
        else:
            scores.append(5.0)

    if not scores:
        return 5.0
    return round(sum(scores) / len(scores), 2)


def score_convergence(
    news: float,
    sentiment: float,
    fii_dii: float,
    macro: float,
    *,
    bullish_threshold: float = 5.5,
    bearish_threshold: float = 4.5,
) -> float:
    """Score 0-10 measuring directional agreement across all four signals.

    Smooth gradient — each "leaning bullish" dimension (score > 5.5) lifts
    convergence by 1.25; each "leaning bearish" (< 4.5) drops it by 1.25.
    Result clipped to [0, 10].

    Why this changed (2026-05-08): the prior step-function ("≥3 bullish to
    move off 5.0") meant convergence stayed neutral whenever fewer than 3
    of the 4 dimensions had real-and-strong signal. With sparse data
    (news + sentiment populated, fii_dii + macro often defaulted to 5.0),
    convergence was always 5.0 — and at 1.5× weight it dragged composites
    down hard. The smoother gradient still rewards full agreement (4
    bullish → 10.0) but no longer punishes 2-of-4 alignment.

    The thresholds were also lowered (>6.0 → >5.5 bullish, <4.0 → <4.5
    bearish) so "leaning" signals participate, not just "screaming" ones.
    """
    scores = [news, sentiment, fii_dii, macro]
    n = len(scores)
    bullish = sum(1 for s in scores if s > bullish_threshold)
    bearish = sum(1 for s in scores if s < bearish_threshold)
    delta = (bullish - bearish) * (5.0 / n)
    return round(max(0.0, min(10.0, 5.0 + delta)), 2)


def compute_composite(
    news: float,
    sentiment: float,
    fii_dii: float,
    macro: float,
    convergence: float,
    *,
    w_news: float = 1.0,
    w_sentiment: float = 1.0,
    w_fii_dii: float = 0.8,
    w_macro: float = 0.8,
    w_convergence: float = 1.5,
    policy_event: float | None = None,
    w_policy_event: float = 0.6,
    regime_bias: float = 0.0,
) -> float:
    """Weighted average of dimension scores, normalized to 0-10, with optional
    policy_event term and a global regime_bias offset.

    `policy_event` is a 0-10 score for sector-specific election/policy impact.
    `regime_bias` is a global offset (-2 to +2) applied uniformly to every
    candidate on macro/event days; e.g. +1.5 across the board on a strong
    pro-business election outcome.
    """
    weights = [(news, w_news), (sentiment, w_sentiment), (fii_dii, w_fii_dii),
               (macro, w_macro), (convergence, w_convergence)]
    if policy_event is not None:
        weights.append((policy_event, w_policy_event))

    total_weight = sum(w for _, w in weights)
    weighted = sum(s * w for s, w in weights)
    base = weighted / total_weight if total_weight > 0 else 5.0
    return round(max(0.0, min(10.0, base + regime_bias)), 2)


def score_policy_event(
    sector: str | None,
    policy_articles: list[dict],
) -> float:
    """Per-sector election/policy bias from `is_policy_related` articles.

    `policy_articles` is a list of dicts with keys:
      sectors_mentioned (list[str])  — canonicalised sector names
      market_sentiment  (str)        — bullish | bearish | neutral

    Returns 0-10 (5.0 = neutral / no relevant articles).
    """
    if not sector or not policy_articles:
        return 5.0

    bullish = bearish = 0
    for art in policy_articles:
        sectors = [str(s) for s in (art.get("sectors_mentioned") or [])]
        if sector not in sectors:
            continue
        s = (art.get("market_sentiment") or "").strip().lower()
        if s == "bullish":
            bullish += 1
        elif s == "bearish":
            bearish += 1

    total = bullish + bearish
    if total == 0:
        return 5.0
    ratio = (bullish - bearish) / total  # -1..+1
    return round(5.0 + ratio * 5.0, 2)


def compute_regime_bias(policy_articles: list[dict]) -> float:
    """Compute a global market regime offset from policy/macro articles.

    Returns a value in roughly [-2, +2]. Applied as a constant additive offset
    to every Phase 2 candidate's composite score on macro/event days.
    """
    if not policy_articles:
        return 0.0

    bullish = sum(
        1 for a in policy_articles
        if (a.get("market_sentiment") or "").strip().lower() == "bullish"
    )
    bearish = sum(
        1 for a in policy_articles
        if (a.get("market_sentiment") or "").strip().lower() == "bearish"
    )
    total = bullish + bearish
    if total < 3:  # need a minimum signal to apply a regime bias
        return 0.0
    ratio = (bullish - bearish) / total  # -1..+1
    # Scale to ±2.0 max, dampened by sqrt(total)/5 so a single hot article
    # doesn't shift the whole market.
    import math
    confidence = min(1.0, math.sqrt(total) / 5.0)
    return round(ratio * 2.0 * confidence, 2)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _get_phase1_candidates(session, run_date: date) -> list[tuple[str, str]]:
    """Return (instrument_id, symbol) for Phase 1 passing candidates."""
    result = await session.execute(
        select(FNOCandidate.instrument_id, Instrument.symbol)
        .join(Instrument, FNOCandidate.instrument_id == Instrument.id)
        .where(
            FNOCandidate.run_date == run_date,
            FNOCandidate.phase == 1,
            FNOCandidate.passed_liquidity == True,  # noqa: E712
        )
    )
    return [(str(r.instrument_id), r.symbol) for r in result.all()]


async def _get_recent_pct_change(
    session,
    instrument_id: str,
) -> float | None:
    """Latest close vs prior close from price_daily for one instrument.

    Returns ``None`` when fewer than 2 non-null-close rows are available
    (used by ``score_fii_dii_for_instrument`` to fall back to the raw
    market-wide score).
    """
    from src.models.price import PriceDaily
    # Filter `> 0` rather than just isnot(None): a suspended-trading row
    # can carry close=0 and would silently divide-by-zero or produce
    # spurious ±100% moves below.
    rows = (await session.execute(
        select(PriceDaily.close)
        .where(
            PriceDaily.instrument_id == instrument_id,
            PriceDaily.close > 0,
        )
        .order_by(PriceDaily.date.desc())
        .limit(2)
    )).all()
    if len(rows) < 2:
        return None
    latest, prior = float(rows[0].close), float(rows[1].close)
    if prior <= 0 or latest <= 0:  # belt and suspenders
        return None
    return round((latest - prior) / prior * 100, 4)


async def _get_news_counts(
    session,
    instrument_id: str,
    lookback_hours: int,
    *,
    as_of: datetime | None = None,
) -> tuple[int, int]:
    """Return (bullish_count, bearish_count) from signals in the lookback window.

    When ``as_of`` is supplied (historical replay), the window is
    ``[as_of - lookback, as_of]`` so signals created AFTER the replay
    timestamp don't leak future information into the score. Default
    behaviour (None) is unchanged: window ends at ``now()``.
    """
    from src.models.signal import Signal
    upper = as_of if as_of is not None else datetime.now(tz=timezone.utc)
    cutoff = upper - timedelta(hours=lookback_hours)

    result = await session.execute(
        select(Signal.action, func.count(Signal.id))
        .where(
            Signal.instrument_id == instrument_id,
            Signal.created_at >= cutoff,
            Signal.created_at <= upper,
        )
        .group_by(Signal.action)
    )
    rows = result.all()
    bullish = sum(count for action, count in rows if action in ("BUY", "BULLISH"))
    bearish = sum(count for action, count in rows if action in ("SELL", "BEARISH"))
    return bullish, bearish


async def get_latest_fii_dii(session) -> tuple[float | None, float | None]:
    """Return (fii_net_cr, dii_net_cr) from the most recent fii_dii raw_content,
    or (None, None) if no row exists or the JSON cannot be parsed.

    Public (no underscore prefix) because both Phase 2 (here) and Phase 3
    (``thesis_synthesizer.run_phase3``) need this same data and must agree
    on the contract. Originally a private copy-pasted helper in both
    modules; consolidated 2026-05-08 to a single canonical implementation
    after a code review caught the divergence drift.

    The previous version silently returned (0.0, 0.0) on any failure,
    which made "no FII/DII data today" indistinguishable from "FII and
    DII both flat at 0 Cr" in the LLM prompt. Returning None lets the
    caller render an explicit "(data unavailable)" line so the LLM
    doesn't apply the FII/DII alignment rule against zeros.
    """
    import json
    result = await session.execute(
        select(RawContent.content_text)
        .where(RawContent.media_type == "fii_dii")
        .order_by(RawContent.fetched_at.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None:
        logger.warning(
            "fno.catalyst: no fii_dii rows in raw_content — Phase 2 will treat "
            "FII/DII as unavailable (downstream LLM prompt renders '(data unavailable)')"
        )
        return None, None
    try:
        data = json.loads(row)
        fii = data.get("fii_net_cr")
        dii = data.get("dii_net_cr")
        if fii is None or dii is None:
            logger.warning(
                f"fno.catalyst: fii_dii row present but keys missing "
                f"(fii_net_cr={fii}, dii_net_cr={dii}) — treating as unavailable"
            )
            return None, None
        return float(fii), float(dii)
    except Exception as exc:
        logger.warning(
            f"fno.catalyst: fii_dii row parse failed ({exc}) — treating as unavailable"
        )
        return None, None


async def _get_latest_macro(session) -> dict[str, float]:
    """Return {macro_name: change_pct} from the most recent macro raw_content rows."""
    import json
    from sqlalchemy import text

    # Get the latest fetched_at for each macro_name via subquery
    result = await session.execute(
        select(RawContent.content_text)
        .where(RawContent.media_type == "macro")
        .order_by(RawContent.fetched_at.desc())
        .limit(50)
    )
    rows = result.scalars().all()

    snapshots: dict[str, float] = {}
    for raw in rows:
        try:
            data = json.loads(raw)
            name = data.get("macro_name")
            chg = data.get("change_pct")
            if name and chg is not None and name not in snapshots:
                snapshots[name] = float(chg)
        except Exception:
            continue
    return snapshots


async def _get_policy_articles(
    session,
    lookback_hours: int,
    *,
    as_of: datetime | None = None,
) -> list[dict]:
    """Pull `is_policy_related=true` extractions from the last `lookback_hours`.

    Returns a list of dicts with sectors_mentioned + market_sentiment, each
    canonicalised. Reads RawContent.extraction_result JSON column written by
    LLMExtractor._mark_processed.

    Honors ``as_of`` for historical replay so policy articles fetched
    AFTER the replay timestamp are excluded.
    """
    from src.extraction.llm_extractor import _canonicalise_sector
    upper = as_of if as_of is not None else datetime.now(tz=timezone.utc)
    cutoff = upper - timedelta(hours=lookback_hours)
    result = await session.execute(
        select(RawContent.extraction_result)
        .where(
            RawContent.fetched_at >= cutoff,
            RawContent.fetched_at <= upper,
            RawContent.is_processed == True,  # noqa: E712
            RawContent.extraction_result.is_not(None),
        )
        .limit(500)
    )
    articles: list[dict] = []
    for (er,) in result.all():
        if not isinstance(er, dict):
            continue
        if not er.get("is_policy_related"):
            continue
        raw_sectors = er.get("sectors_mentioned") or []
        canon = [
            s for s in (_canonicalise_sector(str(x)) for x in raw_sectors) if s
        ]
        if not canon:
            continue
        articles.append({
            "sectors_mentioned": canon,
            "market_sentiment": (er.get("market_sentiment") or "").lower(),
        })
    return articles


async def _get_sentiment_score(session) -> float:
    """Retrieve the latest market_sentiment score (0-10, 5=neutral if none)."""
    from src.models.content import RawContent
    result = await session.execute(
        select(RawContent.content_text)
        .where(RawContent.media_type == "sentiment")
        .order_by(RawContent.fetched_at.desc())
        .limit(1)
    )
    raw = result.scalar_one_or_none()
    if raw is None:
        return 5.0
    try:
        import json
        data = json.loads(raw)
        return float(data.get("score", 5.0))
    except Exception:
        return 5.0


async def _upsert_phase2_candidate(
    session,
    instrument_id: str,
    run_date: date,
    news: float,
    sentiment: float,
    fii_dii: float | None,
    macro: float,
    convergence: float,
    composite: float,
    config_version: str,
    *,
    instrument_tier: str | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> None:
    """Upsert a Phase 2 candidate row.

    ``fii_dii`` may be ``None`` to indicate the underlying FII/DII data
    was unavailable for this run — Phase 3 reads None and renders
    "(data unavailable)" rather than a misleading "5/10".

    ``instrument_tier`` snapshots the row's tier ('T1' / 'T2' / None) so a
    historical replay carries the tier that was in effect on the run_date.
    """
    fii_dii_decimal = Decimal(str(fii_dii)) if fii_dii is not None else None
    stmt = pg_insert(FNOCandidate).values(
        instrument_id=instrument_id,
        run_date=run_date,
        phase=2,
        news_score=Decimal(str(news)),
        sentiment_score=Decimal(str(sentiment)),
        fii_dii_score=fii_dii_decimal,
        macro_align_score=Decimal(str(macro)),
        convergence_score=Decimal(str(convergence)),
        composite_score=Decimal(str(composite)),
        config_version=config_version,
        instrument_tier=instrument_tier,
        dryrun_run_id=dryrun_run_id,
        created_at=datetime.now(tz=timezone.utc),
    ).on_conflict_do_update(
        index_elements=["instrument_id", "run_date", "phase"],
        set_={
            "news_score": Decimal(str(news)),
            "sentiment_score": Decimal(str(sentiment)),
            "fii_dii_score": fii_dii_decimal,
            "macro_align_score": Decimal(str(macro)),
            "convergence_score": Decimal(str(convergence)),
            "composite_score": Decimal(str(composite)),
            "config_version": config_version,
            "instrument_tier": instrument_tier,
        }
    )
    await session.execute(stmt)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

@dataclass
class Phase2Result:
    instrument_id: str
    symbol: str
    passed: bool
    news_score: float = 5.0
    sentiment_score: float = 5.0
    # ``fii_dii_score`` is ``None`` when the underlying market FII/DII data
    # was unavailable for the run — distinguishes "data missing" from "real
    # neutral". Phase 3 reads the same shape from FNOCandidate.fii_dii_score
    # and renders "(data unavailable)" in the LLM prompt.
    fii_dii_score: float | None = 5.0
    macro_align_score: float = 5.0
    convergence_score: float = 5.0
    composite_score: float = 5.0


async def run_phase2(
    run_date: date | None = None,
    *,
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> list[Phase2Result]:
    """Run Phase 2 catalyst scoring for all Phase-1 passing instruments.

    Returns list of Phase2Result; instruments meeting min_composite_score
    have a phase=2 fno_candidates row written.

    ``as_of`` (CLAUDE.md convention): when supplied, all news / policy
    lookback windows are anchored on it so a historical replay sees only
    information available before that moment. ``dryrun_run_id`` is
    stamped on the written rows for replay scoping.
    """
    if run_date is None:
        run_date = (as_of.date() if as_of is not None else date.today())

    cfg = _settings
    min_score = cfg.fno_phase2_min_composite_score
    lookback = cfg.fno_phase2_news_lookback_hours
    config_ver = cfg.fno_ranker_version

    async with session_scope() as session:
        candidates = await _get_phase1_candidates(session, run_date)
        fii_net, dii_net = await get_latest_fii_dii(session)
        macro_snaps = await _get_latest_macro(session)
        sentiment = await _get_sentiment_score(session)
        policy_articles = await _get_policy_articles(session, lookback, as_of=as_of)

    # Track whether FII/DII data is real or missing. When missing, we
    # compute the composite using a neutral 5.0 contribution (so downstream
    # math doesn't break) but write None to FNOCandidate.fii_dii_score so
    # Phase 3 can render "(data unavailable)" instead of misleading "5/10".
    fii_dii_available = fii_net is not None and dii_net is not None

    regime_bias = compute_regime_bias(policy_articles)
    if regime_bias != 0.0:
        logger.info(
            f"fno.catalyst: regime_bias={regime_bias:+.2f} from "
            f"{len(policy_articles)} policy/macro articles"
        )

    if not candidates:
        logger.warning("fno.catalyst: no Phase-1 candidates to score")
        return []

    results: list[Phase2Result] = []

    # Resolve tier label once per row at write time. tier_manager is the
    # source of truth — we don't recompute from volume here.
    from src.fno.tier_manager import get_tier_label

    for inst_id, symbol in candidates:
        try:
            async with session_scope() as session:
                bullish_ct, bearish_ct = await _get_news_counts(
                    session, inst_id, lookback, as_of=as_of
                )
                inst_row = await session.execute(
                    select(Instrument.sector).where(Instrument.id == inst_id)
                )
                sector = inst_row.scalar_one_or_none()
                # Per-stock pct_change for the FII/DII alignment proxy
                stock_pct = await _get_recent_pct_change(session, inst_id)
                tier_label = await get_tier_label(inst_id, session=session)

            news_s = score_news(bullish_ct, bearish_ct)
            # Per-instrument FII/DII: market-wide flow modulated by this
            # stock's recent price action. When the underlying flow data
            # is unavailable, fold a neutral 5.0 into the composite math
            # but track the unavailability so we can persist None.
            if fii_dii_available:
                fii_dii_s_real = score_fii_dii_for_instrument(
                    fii_net, dii_net, stock_pct
                )
                fii_dii_s_for_math = fii_dii_s_real
            else:
                fii_dii_s_real = None
                fii_dii_s_for_math = 5.0
            macro_s = score_macro(sector, macro_snaps)
            policy_s = score_policy_event(sector, policy_articles)
            conv_s = score_convergence(
                news_s, sentiment, fii_dii_s_for_math, macro_s
            )
            comp_s = compute_composite(
                news_s, sentiment, fii_dii_s_for_math, macro_s, conv_s,
                w_news=cfg.fno_phase2_weight_news,
                w_sentiment=cfg.fno_phase2_weight_sentiment,
                w_fii_dii=cfg.fno_phase2_weight_fii_dii,
                w_macro=cfg.fno_phase2_weight_macro,
                w_convergence=cfg.fno_phase2_weight_convergence,
                policy_event=policy_s,
                regime_bias=regime_bias,
            )

            # Bidirectional gate: pass bullish conviction (>= min_score) OR
            # bearish conviction (<= 10 - min_score). Both reach Phase 3 which
            # handles directional theses in either direction.
            deviation = min_score - 5.0  # e.g. 0.5 when min_score=5.5
            passed = abs(comp_s - 5.0) >= deviation
            res = Phase2Result(
                instrument_id=inst_id,
                symbol=symbol,
                passed=passed,
                news_score=news_s,
                sentiment_score=sentiment,
                # Pass the real per-instrument value through (may be None
                # when market FII/DII data was unavailable). The DB row
                # gets the same value via _upsert_phase2_candidate below,
                # so in-memory result and persisted row agree on shape.
                fii_dii_score=fii_dii_s_real,
                macro_align_score=macro_s,
                convergence_score=conv_s,
                composite_score=comp_s,
            )
            results.append(res)

            if passed:
                async with session_scope() as session:
                    # Pass fii_dii_s_real (may be None) so the FNOCandidate
                    # row reflects data availability — NOT fii_dii_s_for_math
                    # which is the neutral 5.0 used only for composite math.
                    await _upsert_phase2_candidate(
                        session, inst_id, run_date,
                        news_s, sentiment, fii_dii_s_real, macro_s,
                        conv_s, comp_s,
                        config_ver,
                        instrument_tier=tier_label,
                        dryrun_run_id=dryrun_run_id,
                    )
                direction = "BULLISH" if comp_s >= min_score else "BEARISH"
                logger.debug(f"fno.catalyst: {symbol} PASS [{direction}] composite={comp_s:.1f}")
            else:
                logger.debug(f"fno.catalyst: {symbol} FAIL composite={comp_s:.1f} (neutral band)")

        except Exception as exc:
            logger.warning(f"fno.catalyst: {symbol} error: {exc}")

    passed_count = sum(1 for r in results if r.passed)
    logger.info(
        f"fno.catalyst: Phase 2 complete — {passed_count}/{len(results)} passed for {run_date}"
    )
    return results
