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
) -> float:
    """Score 0-10 measuring directional agreement across all four signals.

    Counts how many dimensions are bullish (>6), bearish (<4), neutral (4-6).
    Strong convergence (all agree) → 10 or 0. No convergence → 5.
    """
    scores = [news, sentiment, fii_dii, macro]
    bullish = sum(1 for s in scores if s > 6.0)
    bearish = sum(1 for s in scores if s < 4.0)

    if bullish >= 3:
        return round(5.0 + (bullish / len(scores)) * 5.0, 2)
    if bearish >= 3:
        return round(5.0 - (bearish / len(scores)) * 5.0, 2)
    return 5.0


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


async def _get_news_counts(
    session,
    instrument_id: str,
    lookback_hours: int,
) -> tuple[int, int]:
    """Return (bullish_count, bearish_count) from signals in the lookback window."""
    from src.models.signal import Signal
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=lookback_hours)

    result = await session.execute(
        select(Signal.action, func.count(Signal.id))
        .where(
            Signal.instrument_id == instrument_id,
            Signal.created_at >= cutoff,
        )
        .group_by(Signal.action)
    )
    rows = result.all()
    bullish = sum(count for action, count in rows if action in ("BUY", "BULLISH"))
    bearish = sum(count for action, count in rows if action in ("SELL", "BEARISH"))
    return bullish, bearish


async def _get_latest_fii_dii(session) -> tuple[float, float]:
    """Return (fii_net_cr, dii_net_cr) from the most recent fii_dii raw_content."""
    import json
    result = await session.execute(
        select(RawContent.content_text)
        .where(RawContent.media_type == "fii_dii")
        .order_by(RawContent.fetched_at.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return 0.0, 0.0
    try:
        data = json.loads(row)
        return float(data.get("fii_net_cr", 0.0)), float(data.get("dii_net_cr", 0.0))
    except Exception:
        return 0.0, 0.0


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


async def _get_policy_articles(session, lookback_hours: int) -> list[dict]:
    """Pull `is_policy_related=true` extractions from the last `lookback_hours`.

    Returns a list of dicts with sectors_mentioned + market_sentiment, each
    canonicalised. Reads RawContent.extraction_result JSON column written by
    LLMExtractor._mark_processed.
    """
    from src.extraction.llm_extractor import _canonicalise_sector
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=lookback_hours)
    result = await session.execute(
        select(RawContent.extraction_result)
        .where(
            RawContent.fetched_at >= cutoff,
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
    fii_dii: float,
    macro: float,
    convergence: float,
    composite: float,
    config_version: str,
) -> None:
    stmt = pg_insert(FNOCandidate).values(
        instrument_id=instrument_id,
        run_date=run_date,
        phase=2,
        news_score=Decimal(str(news)),
        sentiment_score=Decimal(str(sentiment)),
        fii_dii_score=Decimal(str(fii_dii)),
        macro_align_score=Decimal(str(macro)),
        convergence_score=Decimal(str(convergence)),
        composite_score=Decimal(str(composite)),
        config_version=config_version,
        created_at=datetime.now(tz=timezone.utc),
    ).on_conflict_do_update(
        index_elements=["instrument_id", "run_date", "phase"],
        set_={
            "news_score": Decimal(str(news)),
            "sentiment_score": Decimal(str(sentiment)),
            "fii_dii_score": Decimal(str(fii_dii)),
            "macro_align_score": Decimal(str(macro)),
            "convergence_score": Decimal(str(convergence)),
            "composite_score": Decimal(str(composite)),
            "config_version": config_version,
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
    fii_dii_score: float = 5.0
    macro_align_score: float = 5.0
    convergence_score: float = 5.0
    composite_score: float = 5.0


async def run_phase2(run_date: date | None = None) -> list[Phase2Result]:
    """Run Phase 2 catalyst scoring for all Phase-1 passing instruments.

    Returns list of Phase2Result; instruments meeting min_composite_score
    have a phase=2 fno_candidates row written.
    """
    if run_date is None:
        run_date = date.today()

    cfg = _settings
    min_score = cfg.fno_phase2_min_composite_score
    lookback = cfg.fno_phase2_news_lookback_hours
    config_ver = cfg.fno_ranker_version

    async with session_scope() as session:
        candidates = await _get_phase1_candidates(session, run_date)
        fii_net, dii_net = await _get_latest_fii_dii(session)
        macro_snaps = await _get_latest_macro(session)
        sentiment = await _get_sentiment_score(session)
        policy_articles = await _get_policy_articles(session, lookback)

    fii_dii_s = score_fii_dii(fii_net, dii_net)
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

    for inst_id, symbol in candidates:
        try:
            async with session_scope() as session:
                bullish_ct, bearish_ct = await _get_news_counts(session, inst_id, lookback)
                inst_row = await session.execute(
                    select(Instrument.sector).where(Instrument.id == inst_id)
                )
                sector = inst_row.scalar_one_or_none()

            news_s = score_news(bullish_ct, bearish_ct)
            macro_s = score_macro(sector, macro_snaps)
            policy_s = score_policy_event(sector, policy_articles)
            conv_s = score_convergence(news_s, sentiment, fii_dii_s, macro_s)
            comp_s = compute_composite(
                news_s, sentiment, fii_dii_s, macro_s, conv_s,
                w_news=cfg.fno_phase2_weight_news,
                w_sentiment=cfg.fno_phase2_weight_sentiment,
                w_fii_dii=cfg.fno_phase2_weight_fii_dii,
                w_macro=cfg.fno_phase2_weight_macro,
                w_convergence=cfg.fno_phase2_weight_convergence,
                policy_event=policy_s,
                regime_bias=regime_bias,
            )

            passed = comp_s >= min_score
            res = Phase2Result(
                instrument_id=inst_id,
                symbol=symbol,
                passed=passed,
                news_score=news_s,
                sentiment_score=sentiment,
                fii_dii_score=fii_dii_s,
                macro_align_score=macro_s,
                convergence_score=conv_s,
                composite_score=comp_s,
            )
            results.append(res)

            if passed:
                async with session_scope() as session:
                    await _upsert_phase2_candidate(
                        session, inst_id, run_date,
                        news_s, sentiment, fii_dii_s, macro_s, conv_s, comp_s,
                        config_ver,
                    )
                logger.debug(f"fno.catalyst: {symbol} PASS composite={comp_s:.1f}")
            else:
                logger.debug(f"fno.catalyst: {symbol} FAIL composite={comp_s:.1f}<{min_score}")

        except Exception as exc:
            logger.warning(f"fno.catalyst: {symbol} error: {exc}")

    passed_count = sum(1 for r in results if r.passed)
    logger.info(
        f"fno.catalyst: Phase 2 complete — {passed_count}/{len(results)} passed for {run_date}"
    )
    return results
