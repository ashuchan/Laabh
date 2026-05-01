"""
Replace hardcoded VIX threshold with feature-based regime classifier.
"""
from __future__ import annotations

import logging

import pandas as pd

from src.integrations.freqai_inspired.feature_pipeline import (
    Regime,
    classify_regime,
    extract_features,
)

logger = logging.getLogger(__name__)

ALLOWED_REGIMES: frozenset[Regime] = frozenset(
    {"trending_bullish", "trending_bearish", "sideways"}
)
BLOCKED_REGIMES: frozenset[Regime] = frozenset({"high_vol"})


async def is_regime_tradeable(
    ohlcv: pd.DataFrame,
    vix: float,
    pcr: float,
) -> tuple[bool, Regime]:
    """
    Returns (tradeable: bool, regime: Regime).

    Fail-safe: any exception in feature extraction → (False, "high_vol").
    This ensures a data pipeline failure blocks trading rather than permitting it.

    Args:
        ohlcv: Daily OHLCV DataFrame (need ≥26 rows).
        vix:   India VIX. Pass float('nan') if unavailable — will block trading.
        pcr:   Nifty Put-Call Ratio.
    """
    try:
        features = extract_features(ohlcv, vix, pcr)
        regime = classify_regime(features)
        tradeable = regime not in BLOCKED_REGIMES
        logger.debug(f"is_regime_tradeable: regime={regime}, tradeable={tradeable}")
        return tradeable, regime
    except ValueError as exc:
        # Bad data (missing columns, too few rows, NaN values)
        logger.warning(
            f"is_regime_tradeable: feature extraction failed ({exc}) "
            "— blocking trade (fail-safe high_vol)"
        )
        return False, "high_vol"
    except Exception as exc:
        # Unexpected failure — never let it propagate into strategy logic
        logger.error(
            f"is_regime_tradeable: unexpected error ({exc}) "
            "— blocking trade (fail-safe high_vol)",
            exc_info=True,
        )
        return False, "high_vol"
