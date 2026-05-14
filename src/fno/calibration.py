"""LLM-feature calibration — Phase 2 of the LLM-as-feature-generator plan.

Plan reference: docs/llm_feature_generator/implementation_plan.md §2.

For each (prompt_version, phase, feature_name, instrument_tier) tuple, fit a
calibration curve that maps a raw LLM score → expected ``outcome_z``. The
weekly job runs Sundays 22:00 IST and only promotes a new model when it
improves ECE by ≥5% without worsening residual variance by >5%.

Method ladder (plan §2.1):
  - N < 100         → no fit, no model active.
  - 100 ≤ N < 500   → Platt scaling: ``tanh(a · raw + b)``.
  - N ≥ 500         → isotonic regression via PAVA (monotone step function).

IPS reweighting (plan §2.2): each observation is weighted by ``1 /
bandit_arm_propensity`` clipped to ``[0.1, 10]``. Counterfactual rows are
further multiplied by 0.3 to discount un-traded outcomes.

Walk-forward CV (plan §2.3): three expanding-window folds with a 1-day
embargo to prevent leakage from adjacent observations.

The module intentionally avoids sklearn/scipy. PAVA and Platt are simple
enough to implement directly; this keeps the dependency footprint flat and
the calibration job free of import-time cost.
"""
from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import numpy as np
from loguru import logger
from sqlalchemy import text, update

from src.db import session_scope
from src.models.llm_decision_log import LLMCalibrationModel, LLMDecisionLog

# Project root — anchors filesystem outputs against the same path the
# dashboard reads from (apps/static/calibration). Resolved at import time
# so the scheduler's cwd (which may be C:\Windows\System32 under Task
# Scheduler) doesn't affect where PNGs land.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# matplotlib backend — set ONCE at module import so any later matplotlib
# use in this process (including by tests / dashboards that happen to
# share an interpreter) inherits the headless backend. The try/except
# keeps environments without matplotlib working — fits still run, only
# the reliability PNG is skipped.
try:
    import matplotlib   # noqa: E402  — must precede pyplot
    matplotlib.use("Agg")
    import matplotlib.pyplot as _plt   # noqa: E402
    _MATPLOTLIB_AVAILABLE = True
except Exception:   # broad — matplotlib's import can fail with non-ImportError
    _plt = None
    _MATPLOTLIB_AVAILABLE = False


# Plan §2.1
_MIN_N_FOR_FIT = 100
_ISOTONIC_THRESHOLD = 500

# Plan §2.2
_IPS_WEIGHT_CLIP = (0.1, 10.0)
_COUNTERFACTUAL_WEIGHT_MULT = 0.3

# Plan §2.4
_ECE_IMPROVE_MIN = 0.05
_RESID_VAR_WORSEN_MAX = 0.05
_OUTCOME_Z_CLIP = 3.0    # clip z to ±3σ to bound the calibration target

# Walk-forward CV: 3 folds, 10-day test slabs, 1-day embargo.
_FOLD_TEST_DAYS = 10
_FOLD_EMBARGO_DAYS = 1
_N_FOLDS = 3

_FEATURE_TO_COLUMN: dict[str, str] = {
    "directional_conviction": "directional_conviction",
    "raw_confidence": "raw_confidence",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class CalibrationModel:
    """In-memory representation of a fitted calibration curve."""

    method: str                          # 'platt' | 'isotonic'
    params: dict                         # platt: {a, b}; isotonic: {x_knots, y_knots}
    n_observations: int
    cv_ece: float | None
    cv_residual_var: float | None
    feature_name: str
    prompt_version: str
    phase: str
    instrument_tier: str
    fitted_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


def apply_calibration(model: CalibrationModel, raw_score: float) -> float:
    """Apply the calibration map to a raw score. Output is bounded to ±3."""
    raw = float(raw_score)
    if model.method == "platt":
        a = float(model.params["a"])
        b = float(model.params["b"])
        return _OUTCOME_Z_CLIP * math.tanh(a * raw + b)
    if model.method == "isotonic":
        xs = np.array(model.params["x_knots"], dtype=float)
        ys = np.array(model.params["y_knots"], dtype=float)
        if xs.size == 0:
            return 0.0
        return float(np.interp(raw, xs, ys))
    raise ValueError(f"unknown calibration method: {model.method}")


async def fit_calibration(
    feature_name: str,
    prompt_version: str,
    phase: str,
    instrument_tier: str = "pooled",
    *,
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> CalibrationModel | None:
    """Fit one calibration curve. Returns None when sample size is short.

    The function does NOT activate the resulting model — the caller decides
    via :func:`promote_if_better` whether the new fit beats the current
    active one on ECE without giving up too much residual variance.

    Side effect: emits a reliability-diagram PNG to
    ``apps/static/calibration/<feature>_<prompt>_<phase>_<tier>_<ts>.png``
    when a fit completes (review fix P3 #10, plan §2.4). Failures to
    write are non-fatal — the fit returns normally.
    """
    rows = await _load_calibration_rows(
        feature_name=feature_name,
        prompt_version=prompt_version,
        phase=phase,
        instrument_tier=instrument_tier,
        as_of=as_of,
        dryrun_run_id=dryrun_run_id,
    )
    if len(rows) < _MIN_N_FOR_FIT:
        logger.info(
            f"calibration: skip {prompt_version}/{phase}/{feature_name}/"
            f"{instrument_tier} — only {len(rows)} rows (need {_MIN_N_FOR_FIT})"
        )
        return None

    x = np.array([r["raw"] for r in rows], dtype=float)
    y = np.clip(
        np.array([r["outcome_z"] for r in rows], dtype=float),
        -_OUTCOME_Z_CLIP, _OUTCOME_Z_CLIP,
    )
    w = np.array([r["weight"] for r in rows], dtype=float)

    method = "platt" if len(rows) < _ISOTONIC_THRESHOLD else "isotonic"
    fit_full = _fit_one(method, x, y, w)
    cv_ece, cv_resid = _walk_forward_metrics(rows, method=method)
    model = CalibrationModel(
        method=method,
        params=fit_full,
        n_observations=len(rows),
        cv_ece=cv_ece,
        cv_residual_var=cv_resid,
        feature_name=feature_name,
        prompt_version=prompt_version,
        phase=phase,
        instrument_tier=instrument_tier,
    )
    _emit_reliability_png(rows, model=model)
    return model


async def promote_if_better(
    candidate: CalibrationModel,
    *,
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> bool:
    """Persist ``candidate`` and activate it only if metrics beat the
    currently-active model on the same key.

    Promotion criteria (plan §2.4):
      - ECE improves by ≥ 5% (relative).
      - Residual variance does not worsen by more than 5% (relative).
    """
    persisted_id = await _persist_model(candidate)

    current = await _load_active_model(
        feature_name=candidate.feature_name,
        prompt_version=candidate.prompt_version,
        phase=candidate.phase,
        instrument_tier=candidate.instrument_tier,
    )
    if current is None:
        await _set_active(persisted_id, candidate)
        logger.info(
            f"calibration: ACTIVATED first model — {candidate.feature_name}/"
            f"{candidate.prompt_version}/{candidate.phase}/{candidate.instrument_tier} "
            f"(ECE={candidate.cv_ece}, RV={candidate.cv_residual_var})"
        )
        return True

    ece_now = candidate.cv_ece if candidate.cv_ece is not None else float("inf")
    ece_prev = current.cv_ece if current.cv_ece is not None else float("inf")
    rv_now = candidate.cv_residual_var if candidate.cv_residual_var is not None else float("inf")
    rv_prev = current.cv_residual_var if current.cv_residual_var is not None else float("inf")

    ece_relative_drop = (ece_prev - ece_now) / max(ece_prev, 1e-9)
    rv_relative_rise = (rv_now - rv_prev) / max(rv_prev, 1e-9)

    if ece_relative_drop >= _ECE_IMPROVE_MIN and rv_relative_rise <= _RESID_VAR_WORSEN_MAX:
        await _set_active(persisted_id, candidate)
        logger.info(
            f"calibration: PROMOTED — ECE {ece_prev:.4f}→{ece_now:.4f} "
            f"(rel drop {ece_relative_drop:.1%}), RV {rv_prev:.4f}→{rv_now:.4f}"
        )
        return True

    logger.info(
        f"calibration: kept current — new ECE drop {ece_relative_drop:.1%} "
        f"< {_ECE_IMPROVE_MIN:.0%} or RV rose {rv_relative_rise:.1%} "
        f"> {_RESID_VAR_WORSEN_MAX:.0%}"
    )
    return False


async def run_weekly_calibration(
    *,
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Top-level entry — fit + maybe-promote every (feature, tier) pair.

    Scoped today to (v10_continuous, fno_thesis). Adds rows for tier
    'pooled' first so a per-tier failure does not block the safe-default
    pooled model.
    """
    promoted = skipped = 0
    for feature_name in ("directional_conviction", "raw_confidence"):
        for tier in ("pooled", "T1", "T2"):
            try:
                model = await fit_calibration(
                    feature_name=feature_name,
                    prompt_version="v10_continuous",
                    phase="fno_thesis",
                    instrument_tier=tier,
                    as_of=as_of,
                    dryrun_run_id=dryrun_run_id,
                )
                if model is None:
                    skipped += 1
                    continue
                if await promote_if_better(model, as_of=as_of, dryrun_run_id=dryrun_run_id):
                    promoted += 1
                else:
                    skipped += 1
            except Exception as exc:
                logger.warning(
                    f"calibration: failed fit for {feature_name}/{tier}: {exc!r}"
                )
                skipped += 1
    return {"promoted": promoted, "skipped": skipped}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


async def _load_calibration_rows(
    *,
    feature_name: str,
    prompt_version: str,
    phase: str,
    instrument_tier: str,
    as_of: datetime | None,
    dryrun_run_id: uuid.UUID | None,
) -> list[dict]:
    """Pull (raw, outcome_z, ips_weight, run_date) tuples for the key."""
    column = _FEATURE_TO_COLUMN.get(feature_name)
    if column is None:
        raise ValueError(f"unknown feature_name: {feature_name}")

    # Window: 90-day rolling (plan §0.1 retention comment). 'pooled' tier
    # ignores any tier predicate; per-tier needs a tier lookup we don't yet
    # persist on llm_decision_log, so pooled is the only working option in
    # Phase 0 — T1 / T2 will return zero rows until tier hydration lands.
    # When that lands, this WHERE will gain a tier predicate.
    upper_bound = as_of or datetime.now(tz=timezone.utc)
    lower_bound = upper_bound - timedelta(days=90)

    sql = text(f"""
        SELECT
            l.{column}                    AS raw,
            l.outcome_z                   AS outcome_z,
            l.outcome_class               AS outcome_class,
            l.bandit_arm_propensity       AS propensity,
            l.run_date                    AS run_date
        FROM llm_decision_log l
        WHERE l.prompt_version = :pv
          AND l.phase          = :ph
          AND l.{column}       IS NOT NULL
          AND l.outcome_z      IS NOT NULL
          AND l.outcome_class  IN ('traded', 'counterfactual')
          AND l.as_of >= :lo
          AND l.as_of <= :hi
          AND ((:dryrun_run_id IS NULL  AND l.dryrun_run_id IS NULL)
            OR  l.dryrun_run_id = :dryrun_run_id)
    """)
    async with session_scope() as session:
        result = await session.execute(sql, {
            "pv": prompt_version,
            "ph": phase,
            "lo": lower_bound,
            "hi": upper_bound,
            "dryrun_run_id": str(dryrun_run_id) if dryrun_run_id else None,
        })
        raw_rows = result.mappings().all()

    out: list[dict] = []
    for r in raw_rows:
        prop = float(r["propensity"]) if r["propensity"] is not None else 1.0
        w = 1.0 / max(prop, 1e-9)
        w = max(_IPS_WEIGHT_CLIP[0], min(_IPS_WEIGHT_CLIP[1], w))
        if r["outcome_class"] == "counterfactual":
            w *= _COUNTERFACTUAL_WEIGHT_MULT
        out.append({
            "raw": float(r["raw"]),
            "outcome_z": float(r["outcome_z"]),
            "weight": w,
            "run_date": r["run_date"],
        })
    # Order by run_date so the walk-forward CV folds slice cleanly.
    out.sort(key=lambda d: d["run_date"])
    return out


# ---------------------------------------------------------------------------
# Fit kernels
# ---------------------------------------------------------------------------


def _fit_one(method: str, x: np.ndarray, y: np.ndarray, w: np.ndarray) -> dict:
    if method == "platt":
        return _fit_platt(x, y, w)
    if method == "isotonic":
        return _fit_isotonic_pava(x, y, w)
    raise ValueError(f"unknown method: {method}")


def _fit_platt(x: np.ndarray, y: np.ndarray, w: np.ndarray) -> dict[str, float]:
    """Fit ``y_hat = tanh(a*x + b)`` minimising weighted MSE against y/clip.

    The MSE surface in (a, b) is non-convex but smooth; we use a small
    gradient descent that's fast (≤500 iters) and depends on nothing
    beyond numpy. Initialised from least-squares on the linear part.

    The output is scaled by ``_OUTCOME_Z_CLIP`` at apply time so the
    tanh's ±1 range corresponds to ±3σ outcomes.
    """
    # Linear-LS init: a0 ≈ slope of y vs x, b0 ≈ intercept.
    if np.std(x) > 1e-9:
        a = float(np.cov(x, y, ddof=0)[0, 1] / np.var(x))
        b = float(np.mean(y) - a * np.mean(x))
    else:
        a, b = 1.0, 0.0
    # Normalise targets into tanh's range.
    y_norm = np.clip(y / _OUTCOME_Z_CLIP, -0.999, 0.999)

    lr = 0.05
    for _ in range(500):
        z = a * x + b
        p = np.tanh(z)
        diff = p - y_norm
        # d/da of (1/2)(tanh(z) - y)^2  = (p - y) * sech^2(z) * x = (p - y) * (1 - p^2) * x
        d_a = float(np.mean(w * diff * (1 - p ** 2) * x))
        d_b = float(np.mean(w * diff * (1 - p ** 2)))
        a -= lr * d_a
        b -= lr * d_b
        if abs(d_a) < 1e-7 and abs(d_b) < 1e-7:
            break
    return {"a": a, "b": b}


def _fit_isotonic_pava(x: np.ndarray, y: np.ndarray, w: np.ndarray) -> dict:
    """Pool-Adjacent-Violators isotonic regression with weights.

    Returns ``{x_knots, y_knots}`` defining a piecewise-linear monotonic
    map. The map is forced to be non-decreasing in y, which matches the
    intuition that a higher raw conviction should give a higher expected
    outcome_z; if the data disagrees on average, the algorithm flattens
    the offending segment.
    """
    order = np.argsort(x)
    xs = x[order]
    ys = y[order]
    ws = w[order]

    # Initial blocks — each (xs[i], ys[i], ws[i]) is its own block.
    block_y = ys.astype(float).copy()
    block_w = ws.astype(float).copy()
    block_count = np.ones_like(block_w, dtype=int)

    # PAVA backward merge.
    i = 0
    while i < len(block_y) - 1:
        if block_y[i] > block_y[i + 1]:
            new_w = block_w[i] + block_w[i + 1]
            new_y = (block_y[i] * block_w[i] + block_y[i + 1] * block_w[i + 1]) / new_w
            block_y[i] = new_y
            block_w[i] = new_w
            block_count[i] += block_count[i + 1]
            # Delete entry i+1 by shifting; smaller arrays for clarity.
            block_y = np.delete(block_y, i + 1)
            block_w = np.delete(block_w, i + 1)
            block_count = np.delete(block_count, i + 1)
            if i > 0:
                i -= 1   # may need to re-merge backwards
        else:
            i += 1

    # Build knot lists by emitting one point per block (the midpoint of
    # the block's x-range maps to the pooled y).
    knots_x: list[float] = []
    knots_y: list[float] = []
    cursor = 0
    for size, by in zip(block_count, block_y):
        block_xs = xs[cursor:cursor + size]
        cursor += size
        knots_x.append(float(block_xs.mean()))
        knots_y.append(float(by))

    return {"x_knots": knots_x, "y_knots": knots_y}


# ---------------------------------------------------------------------------
# Walk-forward CV & metrics
# ---------------------------------------------------------------------------


def _walk_forward_metrics(
    rows: list[dict], *, method: str
) -> tuple[float | None, float | None]:
    """Three expanding-window folds with 1-day embargo. Returns mean ECE +
    mean residual variance across folds; None when the dataset is too
    short for at least one fold."""
    if len(rows) < 3 * _FOLD_TEST_DAYS:
        return None, None

    by_day: dict[date, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["run_date"], []).append(r)
    days = sorted(by_day)
    if len(days) < _FOLD_TEST_DAYS * 3:
        return None, None

    eces: list[float] = []
    rvs: list[float] = []
    for fold_idx in range(_N_FOLDS):
        test_end = len(days) - fold_idx * _FOLD_TEST_DAYS
        test_start = test_end - _FOLD_TEST_DAYS
        train_end = test_start - _FOLD_EMBARGO_DAYS
        if train_end <= 0:
            break
        train_days = set(days[:train_end])
        test_days = set(days[test_start:test_end])
        train_rows = [r for d in train_days for r in by_day[d]]
        test_rows = [r for d in test_days for r in by_day[d]]
        if len(train_rows) < _MIN_N_FOR_FIT or not test_rows:
            continue
        params = _fit_one(
            method,
            np.array([r["raw"] for r in train_rows]),
            np.array([r["outcome_z"] for r in train_rows]),
            np.array([r["weight"] for r in train_rows]),
        )
        ece = _ece(test_rows, method=method, params=params)
        rv = _residual_var(test_rows, method=method, params=params)
        eces.append(ece)
        rvs.append(rv)

    if not eces:
        return None, None
    return float(np.mean(eces)), float(np.mean(rvs))


def _predict(raw: float, *, method: str, params: dict) -> float:
    if method == "platt":
        return _OUTCOME_Z_CLIP * math.tanh(params["a"] * raw + params["b"])
    xs = np.array(params["x_knots"], dtype=float)
    ys = np.array(params["y_knots"], dtype=float)
    if xs.size == 0:
        return 0.0
    return float(np.interp(raw, xs, ys))


def _ece(rows: list[dict], *, method: str, params: dict) -> float:
    """Expected Calibration Error over 10 deciles of predicted value."""
    if not rows:
        return float("inf")
    preds = np.array([_predict(r["raw"], method=method, params=params) for r in rows])
    actuals = np.array([r["outcome_z"] for r in rows])
    bins = np.quantile(preds, np.linspace(0, 1, 11))
    bins[-1] += 1e-9
    total = 0.0
    n = len(preds)
    for i in range(10):
        mask = (preds >= bins[i]) & (preds < bins[i + 1])
        if not np.any(mask):
            continue
        gap = abs(float(np.mean(preds[mask])) - float(np.mean(actuals[mask])))
        total += (np.sum(mask) / n) * gap
    return float(total)


def _residual_var(rows: list[dict], *, method: str, params: dict) -> float:
    preds = np.array([_predict(r["raw"], method=method, params=params) for r in rows])
    actuals = np.array([r["outcome_z"] for r in rows])
    return float(np.var(actuals - preds))


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def _persist_model(m: CalibrationModel) -> int:
    """Insert a row into llm_calibration_models, returning the new id."""
    async with session_scope() as session:
        record = LLMCalibrationModel(
            fitted_at=m.fitted_at,
            prompt_version=m.prompt_version,
            phase=m.phase,
            feature_name=m.feature_name,
            instrument_tier=m.instrument_tier,
            method=m.method,
            n_observations=m.n_observations,
            params=m.params,
            cv_ece=m.cv_ece,
            cv_residual_var=m.cv_residual_var,
            is_active=False,
        )
        session.add(record)
        await session.flush()
        return int(record.id)


async def _load_active_model(
    *, feature_name: str, prompt_version: str, phase: str, instrument_tier: str
) -> CalibrationModel | None:
    sql = text("""
        SELECT id, method, params, n_observations, cv_ece, cv_residual_var,
               fitted_at
        FROM llm_calibration_models
        WHERE feature_name    = :fn
          AND prompt_version  = :pv
          AND phase           = :ph
          AND instrument_tier = :ti
          AND is_active       = TRUE
        ORDER BY fitted_at DESC
        LIMIT 1
    """)
    async with session_scope() as session:
        row = (await session.execute(sql, {
            "fn": feature_name, "pv": prompt_version, "ph": phase, "ti": instrument_tier,
        })).first()
    if row is None:
        return None
    params = row.params if isinstance(row.params, dict) else json.loads(row.params)
    return CalibrationModel(
        method=row.method,
        params=params,
        n_observations=row.n_observations,
        cv_ece=float(row.cv_ece) if row.cv_ece is not None else None,
        cv_residual_var=float(row.cv_residual_var) if row.cv_residual_var is not None else None,
        feature_name=feature_name,
        prompt_version=prompt_version,
        phase=phase,
        instrument_tier=instrument_tier,
        fitted_at=row.fitted_at,
    )


async def _set_active(new_id: int, candidate: CalibrationModel) -> None:
    """Atomically deactivate the previous winner and activate ``new_id``."""
    async with session_scope() as session:
        await session.execute(
            update(LLMCalibrationModel)
            .where(
                LLMCalibrationModel.feature_name == candidate.feature_name,
                LLMCalibrationModel.prompt_version == candidate.prompt_version,
                LLMCalibrationModel.phase == candidate.phase,
                LLMCalibrationModel.instrument_tier == candidate.instrument_tier,
                LLMCalibrationModel.is_active.is_(True),
            )
            .values(is_active=False)
        )
        await session.execute(
            update(LLMCalibrationModel)
            .where(LLMCalibrationModel.id == new_id)
            .values(is_active=True)
        )


# ---------------------------------------------------------------------------
# Reliability diagram (review fix P3 #10; plan §2.4)
# ---------------------------------------------------------------------------


def _emit_reliability_png(rows: list[dict], *, model: CalibrationModel) -> None:
    """Render a 10-bin reliability diagram and save it to apps/static/calibration.

    Best-effort: skipped when matplotlib is unavailable (the import attempt
    happens once at module load — see ``_MATPLOTLIB_AVAILABLE``). The PNG
    is a dashboard nicety, not a correctness requirement.

    Output path is anchored to the project root via ``_PROJECT_ROOT`` so
    the file lands where the dashboard reads from, regardless of which
    cwd the scheduler / service was launched in.
    """
    if not _MATPLOTLIB_AVAILABLE or _plt is None:
        logger.debug("calibration: skipping reliability PNG (matplotlib unavailable)")
        return

    try:
        preds = np.array(
            [_predict(r["raw"], method=model.method, params=model.params) for r in rows]
        )
        actuals = np.array([r["outcome_z"] for r in rows])
        bins = np.quantile(preds, np.linspace(0, 1, 11))
        bins[-1] += 1e-9
        bin_pred_means: list[float] = []
        bin_actual_means: list[float] = []
        bin_counts: list[int] = []
        for i in range(10):
            mask = (preds >= bins[i]) & (preds < bins[i + 1])
            if not np.any(mask):
                continue
            bin_pred_means.append(float(np.mean(preds[mask])))
            bin_actual_means.append(float(np.mean(actuals[mask])))
            bin_counts.append(int(np.sum(mask)))

        fig, ax = _plt.subplots(figsize=(5.5, 4.5))
        lo, hi = -_OUTCOME_Z_CLIP, _OUTCOME_Z_CLIP
        ax.plot([lo, hi], [lo, hi], "k--", linewidth=1, label="perfect")
        ax.scatter(
            bin_pred_means, bin_actual_means,
            s=[c * 6 for c in bin_counts], alpha=0.7, label="bin centroid",
        )
        ax.set_xlim(lo, hi)
        ax.set_ylim(lo, hi)
        ax.set_xlabel("predicted outcome_z")
        ax.set_ylabel("realised outcome_z (mean)")
        ece_label = f", ECE={model.cv_ece:.3f}" if model.cv_ece is not None else ""
        ax.set_title(
            f"{model.feature_name} · {model.prompt_version} · {model.instrument_tier}\n"
            f"{model.method}, N={model.n_observations}{ece_label}"
        )
        ax.legend(loc="best", fontsize=8)
        ax.grid(True, alpha=0.3)

        out_dir = _PROJECT_ROOT / "apps" / "static" / "calibration"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = model.fitted_at.strftime("%Y%m%d_%H%M%S")
        fname = (
            f"{model.feature_name}_{model.prompt_version}_{model.phase}_"
            f"{model.instrument_tier}_{ts}.png"
        )
        out_path = out_dir / fname
        fig.tight_layout()
        fig.savefig(out_path, dpi=110)
        _plt.close(fig)
        logger.info(f"calibration: reliability PNG → {out_path}")
    except Exception as exc:
        logger.warning(f"calibration: reliability PNG failed: {exc!r}")
