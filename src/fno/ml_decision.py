"""ML Decision Shadow Mode — parallel prediction alongside the Phase 3 LLM.

Architecture:
  1. FEATURE EXTRACTION: standardized feature vector from Phase 2 FNOCandidate
     + VRP + surface + regime signals. Stored as JSONB for schema-version stability.

  2. PREDICTION: currently a deterministic threshold classifier (baseline_v1)
     that predicts PROCEED when composite score shows strong conviction in either
     direction (|composite - 5.0| >= 0.8) and at least one surface signal aligns.
     This will be replaced with XGBoost (optional import) once 60+ labeled examples
     accumulate from closed trades.

  3. SHADOW RECORDING: ml_shadow_prediction row written alongside every Phase 3
     LLM call. After the LLM responds, the row is updated with llm_decision and
     agreement flag. When a fno_signal closes, outcome_label is written.

  4. AUTO-LABELING: a weekly job scans closed fno_signals and back-fills
     outcome_label on the corresponding ml_shadow_prediction row.

  5. RETRAINING STUB: weekly job checks if >= MIN_TRAINING_EXAMPLES labeled rows
     exist; if so, trains XGBoost and pickles the model to disk. Until then,
     baseline_v1 runs. The stub is implemented but the training is not triggered
     until enough data exists.

Integration:
  - thesis_synthesizer.run_phase3() calls record_prediction() before the LLM
    and update_llm_outcome() after it.
  - scheduler: weekly auto-label job + weekly retrain check.

Feature vector schema (v1):
  {
    composite_score, news_score, sentiment_score, fii_dii_score,
    macro_align_score, convergence_score,
    iv_rank_52w, vrp, vrp_regime_encoded,
    skew_regime_encoded, term_regime_encoded, pcr_near_expiry,
    vix_value, market_regime_encoded,
    days_to_expiry, atm_iv
  }

Regime encoding: trending_bull=2, neutral=1, trending_bear=0, range_high_iv=0.5,
vol_expansion=-1, vol_contraction=1.5
"""
from __future__ import annotations

import json
import math
import os
import pickle
from datetime import date, datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select, text

from src.db import session_scope


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL_VERSION = "baseline_v1"
MIN_TRAINING_EXAMPLES = 60
MODEL_PATH = os.path.join(os.path.dirname(__file__), "../../.cache/ml_model.pkl")

REGIME_ENCODING: dict[str, float] = {
    "trending_bull":   2.0,
    "vol_contraction": 1.5,
    "neutral":         1.0,
    "range_high_iv":   0.5,
    "trending_bear":   0.0,
    "vol_expansion":  -1.0,
}

SKEW_ENCODING: dict[str, float] = {
    "put_skewed":        0.0,
    "flat":              0.5,
    "call_skewed":       1.0,
    "insufficient_data": 0.5,
}

TERM_ENCODING: dict[str, float] = {
    "inverted":     0.0,
    "near_pin":     0.3,
    "flat":         0.5,
    "normal":       1.0,
    "single_expiry": 0.5,
}

VRP_REGIME_ENCODING: dict[str, float] = {
    "cheap": 0.0,
    "fair":  0.5,
    "rich":  1.0,
}


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

def extract_features(
    candidate_id: str,
    composite: float,
    news: float,
    sentiment: float,
    fii_dii: float | None,
    macro: float,
    convergence: float,
    iv_rank: float | None,
    vrp: float | None,
    vrp_regime: str | None,
    skew_regime: str | None,
    term_regime: str | None,
    pcr: float | None,
    vix: float | None,
    market_regime: str | None,
    days_to_expiry: int | None,
    atm_iv: float | None,
) -> dict[str, Any]:
    """Return a normalized feature dictionary for training and inference.

    All missing values are filled with neutral defaults so the feature
    vector always has the same keys regardless of data availability.
    """
    return {
        "composite_score":        composite,
        "composite_deviation":    abs(composite - 5.0),  # conviction strength
        "news_score":             news,
        "sentiment_score":        sentiment,
        "fii_dii_score":          fii_dii if fii_dii is not None else 5.0,
        "fii_dii_available":      1.0 if fii_dii is not None else 0.0,
        "macro_align_score":      macro,
        "convergence_score":      convergence,
        "iv_rank_52w":            iv_rank if iv_rank is not None else 50.0,
        "iv_rank_available":      1.0 if iv_rank is not None else 0.0,
        "vrp":                    vrp * 100.0 if vrp is not None else 0.0,   # in vol points
        "vrp_available":          1.0 if vrp is not None else 0.0,
        "vrp_regime_enc":         VRP_REGIME_ENCODING.get(vrp_regime or "", 0.5),
        "skew_regime_enc":        SKEW_ENCODING.get(skew_regime or "", 0.5),
        "term_regime_enc":        TERM_ENCODING.get(term_regime or "", 0.5),
        "pcr_near_expiry":        pcr if pcr is not None else 1.0,
        "vix_value":              vix if vix is not None else 15.0,
        "market_regime_enc":      REGIME_ENCODING.get(market_regime or "", 1.0),
        "days_to_expiry":         days_to_expiry if days_to_expiry is not None else 7,
        "atm_iv":                 atm_iv * 100.0 if atm_iv is not None else 25.0,  # in %
        "schema_version":         1,
    }


# ---------------------------------------------------------------------------
# Baseline model (deterministic threshold — no ML libraries required)
# ---------------------------------------------------------------------------

def _baseline_predict(features: dict[str, Any]) -> tuple[str, float]:
    """Deterministic baseline classifier using composite score conviction.

    Logic:
      - High conviction bullish (composite >= 5.8) + VRP not cheap → PROCEED
      - High conviction bearish (composite <= 4.2) + VRP not cheap → PROCEED
      - Medium conviction (|dev| >= 0.5) + aligned regime → HEDGE
      - Otherwise → SKIP

    Returns (prediction, confidence).
    """
    composite = features["composite_score"]
    deviation = features["composite_deviation"]
    vrp_enc = features["vrp_regime_enc"]
    regime_enc = features["market_regime_enc"]
    sentiment = features["sentiment_score"]

    # Bullish PROCEED
    if composite >= 5.8 and vrp_enc >= 0.5 and regime_enc >= 1.0:
        conf = min(0.85, 0.55 + (composite - 5.8) * 0.15 + (regime_enc - 1.0) * 0.05)
        return "PROCEED", round(conf, 3)

    # Bearish PROCEED (bidirectional gate)
    if composite <= 4.2 and vrp_enc >= 0.5 and regime_enc <= 0.5:
        conf = min(0.80, 0.55 + (4.2 - composite) * 0.15 + (0.5 - regime_enc) * 0.05)
        return "PROCEED", round(conf, 3)

    # Rich VRP regardless of direction → HEDGE with credit structure
    if vrp_enc >= 1.0 and deviation >= 0.3:
        return "HEDGE", 0.50

    # Medium conviction → HEDGE
    if deviation >= 0.5:
        return "HEDGE", 0.40

    return "SKIP", 0.70


def _ml_predict(features: dict[str, Any]) -> tuple[str, float]:
    """Try XGBoost model; fall back to baseline if not available or not trained."""
    model_path = os.path.abspath(MODEL_PATH)
    if not os.path.exists(model_path):
        return _baseline_predict(features)

    try:
        import xgboost as xgb  # type: ignore[import]
        with open(model_path, "rb") as f:
            model_data = pickle.load(f)

        model: xgb.XGBClassifier = model_data["model"]
        feature_order: list[str] = model_data["feature_order"]
        version: str = model_data.get("version", "xgb_v1")

        row = [features.get(k, 0.0) for k in feature_order]
        proba = model.predict_proba([row])[0]   # [p_unprofitable, p_profitable]
        p_profit = float(proba[1]) if len(proba) > 1 else float(proba[0])
        # Map profit probability to a trading decision (calibrated thresholds)
        if p_profit >= 0.55:
            prediction, confidence = "PROCEED", p_profit
        elif p_profit >= 0.40:
            prediction, confidence = "HEDGE", p_profit
        else:
            prediction, confidence = "SKIP", 1.0 - p_profit
        return prediction, round(confidence, 3)

    except Exception as exc:
        logger.debug(f"ml_decision: XGBoost prediction failed ({exc!r}), using baseline")
        return _baseline_predict(features)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def record_prediction(
    candidate_id: str | None,
    instrument_id: str,
    run_date: date,
    features: dict[str, Any],
) -> tuple[str, float, str]:
    """Compute and persist a shadow prediction. Returns (prediction, confidence, row_id)."""
    prediction, confidence = _ml_predict(features)

    try:
        async with session_scope() as session:
            result = await session.execute(text("""
                INSERT INTO ml_shadow_prediction
                    (fno_candidate_id, instrument_id, run_date,
                     model_version, feature_vector, ml_prediction, ml_confidence)
                VALUES
                    (:cid, :iid, :rd, :mv, CAST(:fv AS jsonb), :pred, :conf)
                ON CONFLICT (fno_candidate_id) DO UPDATE SET
                    ml_prediction = EXCLUDED.ml_prediction,
                    ml_confidence = EXCLUDED.ml_confidence,
                    model_version = EXCLUDED.model_version,
                    feature_vector = EXCLUDED.feature_vector
                RETURNING id::text
            """), {
                "cid": candidate_id,
                "iid": instrument_id,
                "rd": run_date,
                "mv": MODEL_VERSION,
                "fv": json.dumps(features),
                "pred": prediction,
                "conf": confidence,
            })
            row_id = result.scalar_one_or_none() or ""
    except Exception as exc:
        logger.debug(f"ml_decision: DB write failed: {exc!r}")
        row_id = ""

    return prediction, confidence, row_id


async def update_llm_outcome(candidate_id: str | None, llm_decision: str, llm_confidence: float) -> None:
    """After Phase 3 LLM call, record the LLM decision and compute agreement."""
    if candidate_id is None:
        return
    try:
        async with session_scope() as session:
            await session.execute(text("""
                UPDATE ml_shadow_prediction
                SET llm_decision  = CAST(:ld AS varchar),
                    llm_confidence = :lc,
                    agreed = (ml_prediction = CAST(:ld AS varchar))
                WHERE fno_candidate_id = :cid
            """), {"ld": llm_decision, "lc": llm_confidence, "cid": candidate_id})
    except Exception as exc:
        logger.debug(f"ml_decision: outcome update failed: {exc!r}")


async def label_closed_signals() -> int:
    """Back-fill outcome_label for ml_shadow_prediction rows where the trade closed.

    Joins through fno_candidates → fno_signals to find final_pnl.
    Returns the count of rows newly labeled.
    """
    try:
        async with session_scope() as session:
            result = await session.execute(text("""
                UPDATE ml_shadow_prediction msp
                SET outcome_pnl   = sig.final_pnl,
                    outcome_label = CASE WHEN sig.final_pnl > 0 THEN 1 ELSE 0 END,
                    labeled_at    = NOW()
                FROM fno_candidates cand
                JOIN fno_signals sig ON sig.candidate_id = cand.id
                WHERE msp.fno_candidate_id = cand.id
                  AND msp.outcome_label IS NULL
                  AND sig.final_pnl IS NOT NULL
                  AND sig.closed_at IS NOT NULL
                RETURNING msp.id
            """))
            labeled = len(result.fetchall())
        if labeled > 0:
            logger.info(f"ml_decision: auto-labeled {labeled} shadow predictions")
        return labeled
    except Exception as exc:
        logger.warning(f"ml_decision: auto-labeling failed: {exc!r}")
        return 0


async def check_retrain() -> bool:
    """Check if enough labeled examples exist to trigger retraining.

    Returns True if retraining was attempted. Actual training requires XGBoost
    to be installed and MIN_TRAINING_EXAMPLES labeled rows.
    """
    try:
        async with session_scope() as session:
            count = (await session.execute(text("""
                SELECT COUNT(*) FROM ml_shadow_prediction WHERE outcome_label IS NOT NULL
            """))).scalar_one()
        count = int(count)
    except Exception:
        return False

    if count < MIN_TRAINING_EXAMPLES:
        logger.info(
            f"ml_decision: {count}/{MIN_TRAINING_EXAMPLES} labeled examples — "
            f"retraining deferred"
        )
        return False

    logger.info(f"ml_decision: {count} labeled examples — attempting retraining")
    return await _retrain(count)


async def _retrain(n_examples: int) -> bool:
    """Train XGBoost model on labeled examples. No-op if XGBoost not installed."""
    try:
        import xgboost as xgb  # type: ignore[import]
    except ImportError:
        logger.info("ml_decision: XGBoost not installed — install with: pip install xgboost")
        return False

    try:
        async with session_scope() as session:
            rows = (await session.execute(text("""
                SELECT feature_vector, outcome_label
                FROM ml_shadow_prediction
                WHERE outcome_label IS NOT NULL
                ORDER BY labeled_at DESC
                LIMIT 500
            """))).fetchall()

        if not rows:
            return False

        feature_order = [k for k in rows[0].feature_vector.keys() if k != "schema_version"]
        X = [[r.feature_vector.get(k, 0.0) for k in feature_order] for r in rows]
        y = [int(r.outcome_label) for r in rows]  # 0=loss, 1=profit

        # Binary for now (profitable vs not). Multi-class (PROCEED/HEDGE/SKIP) requires
        # outcome labels by decision type — deferred until more data exists.
        model = xgb.XGBClassifier(
            n_estimators=100, max_depth=4, learning_rate=0.1,
            use_label_encoder=False, eval_metric="logloss",
        )
        model.fit(X, y)

        # NOTE on label semantics: outcome_label is 1=profitable / 0=unprofitable.
        # The model predicts P(profitable | features). At inference time we map:
        #   p_profit >= 0.55 → PROCEED, >= 0.40 → HEDGE, else → SKIP.
        # We do NOT store raw SKIP/PROCEED labels because the binary target (profit)
        # is orthogonal to the decision type. A HEDGE that earns is label=1 too.
        os.makedirs(os.path.dirname(os.path.abspath(MODEL_PATH)), exist_ok=True)
        with open(MODEL_PATH, "wb") as f:
            pickle.dump({
                "model": model,
                "feature_order": feature_order,
                "target": "profitable",          # 0=unprofitable, 1=profitable
                "version": f"xgb_v1_{n_examples}ex",
                "trained_at": datetime.now(tz=timezone.utc).isoformat(),
            }, f)

        logger.info(f"ml_decision: XGBoost retrained on {len(X)} examples → model saved")
        return True

    except Exception as exc:
        logger.warning(f"ml_decision: retraining failed: {exc!r}")
        return False


async def get_agreement_stats(lookback_days: int = 30) -> dict:
    """Return LLM vs ML agreement statistics for monitoring."""
    try:
        async with session_scope() as session:
            row = (await session.execute(text("""
                SELECT
                    COUNT(*) total,
                    SUM(CASE WHEN agreed THEN 1 ELSE 0 END) agreements,
                    SUM(CASE WHEN outcome_label IS NOT NULL THEN 1 ELSE 0 END) labeled,
                    AVG(CASE WHEN outcome_label = 1 AND ml_prediction = 'PROCEED' THEN 1.0
                             WHEN outcome_label = 0 AND ml_prediction = 'PROCEED' THEN 0.0
                             END) ml_proceed_win_rate,
                    AVG(CASE WHEN outcome_label = 1 AND llm_decision = 'PROCEED' THEN 1.0
                             WHEN outcome_label = 0 AND llm_decision = 'PROCEED' THEN 0.0
                             END) llm_proceed_win_rate
                FROM ml_shadow_prediction
                WHERE run_date >= CURRENT_DATE - :days
            """), {"days": lookback_days})).first()
        return dict(row._mapping) if row else {}
    except Exception:
        return {}
