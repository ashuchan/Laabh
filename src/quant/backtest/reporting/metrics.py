"""Distributional + risk metrics on a daily-return series.

Pure-function module — accepts ``list[float]`` of daily returns (decimal,
e.g. 0.005 == 0.5%). No DB, no I/O.

Implemented:
  * Distributional: mean, median, std, skew, kurtosis (excess)
  * Risk: max drawdown, Calmar ratio
  * Performance: Sharpe (annualized), profit factor, win rate
  * Robust: deflated Sharpe ratio (Bailey-López de Prado 2014)
  * Bootstrap: block-bootstrap 95% CI on Sharpe

References:
  * Bailey & López de Prado, "The Deflated Sharpe Ratio: Correcting for
    Selection Bias, Backtest Overfitting and Non-Normality" (2014).
  * López de Prado, "Advances in Financial Machine Learning" (2018), Ch. 11.

Dependencies: ``math``, ``random`` (stdlib only — no scipy or numpy).
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Sequence


# Trading days per year — used for annualisation. NSE convention.
TRADING_DAYS_PER_YEAR = 252


# ---------------------------------------------------------------------------
# Result aggregate
# ---------------------------------------------------------------------------

@dataclass
class MetricsBundle:
    """Headline metrics for one return series.

    All metrics are computed on the same input series; constructing this
    via ``compute_metrics`` ensures consistency.
    """

    n: int
    mean: float
    median: float
    std: float
    skew: float
    kurtosis_excess: float

    sharpe: float
    deflated_sharpe: float
    sharpe_ci_lower: float
    sharpe_ci_upper: float

    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float

    max_drawdown: float
    calmar: float


# ---------------------------------------------------------------------------
# Distributional moments
# ---------------------------------------------------------------------------

def mean(xs: Sequence[float]) -> float:
    n = len(xs)
    return sum(xs) / n if n else 0.0


def median(xs: Sequence[float]) -> float:
    n = len(xs)
    if n == 0:
        return 0.0
    s = sorted(xs)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def stdev(xs: Sequence[float], *, ddof: int = 1) -> float:
    """Sample standard deviation (Bessel's correction by default)."""
    n = len(xs)
    if n - ddof <= 0:
        return 0.0
    mu = mean(xs)
    var = sum((x - mu) ** 2 for x in xs) / (n - ddof)
    return math.sqrt(var)


def skew(xs: Sequence[float]) -> float:
    """Sample skewness (g1 estimator)."""
    n = len(xs)
    if n < 3:
        return 0.0
    mu = mean(xs)
    s = stdev(xs)
    if s == 0:
        return 0.0
    return (sum((x - mu) ** 3 for x in xs) / n) / (s ** 3)


def kurtosis_excess(xs: Sequence[float]) -> float:
    """Sample excess kurtosis (g2 estimator). Normal distribution → 0."""
    n = len(xs)
    if n < 4:
        return 0.0
    mu = mean(xs)
    s = stdev(xs)
    if s == 0:
        return 0.0
    return (sum((x - mu) ** 4 for x in xs) / n) / (s ** 4) - 3.0


# ---------------------------------------------------------------------------
# Sharpe & deflated Sharpe
# ---------------------------------------------------------------------------

def sharpe(xs: Sequence[float], *, periods_per_year: int = TRADING_DAYS_PER_YEAR) -> float:
    """Annualised Sharpe ratio of a return series.

    Uses risk-free = 0 (the Indian repo rate is small enough that the
    distinction is largely cosmetic for short backtests; callers wanting
    excess returns can subtract before passing in).
    """
    if not xs:
        return 0.0
    s = stdev(xs)
    if s == 0:
        return 0.0
    return (mean(xs) / s) * math.sqrt(periods_per_year)


def deflated_sharpe(
    xs: Sequence[float],
    *,
    n_trials: int = 1,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """Deflated Sharpe Ratio (Bailey-López de Prado 2014).

    Adjusts the empirical Sharpe for:
      * Number of trials (multiple-testing inflation)
      * Higher moments (skew, kurtosis) of the return distribution

    Returns a probability in [0, 1] — the probability that the *true*
    Sharpe exceeds 0 given the observed series. Values > 0.95 are
    conventionally considered "significant".

    A value below 0.5 means the strategy looks no better than a sentinel
    that picks the best of N random strategies.
    """
    n = len(xs)
    if n < 4:
        return 0.0
    sr = sharpe(xs, periods_per_year=periods_per_year) / math.sqrt(periods_per_year)
    g1 = skew(xs)
    g2 = kurtosis_excess(xs)

    # Variance of the SR estimator under non-normality (Mertens 2002)
    var_sr = (1.0 + 0.5 * sr * sr - g1 * sr + (g2 / 4.0) * sr * sr) / (n - 1)
    if var_sr <= 0:
        return 0.0

    # Threshold SR_0 — expected max SR under N independent random trials
    # Bailey & López de Prado: SR_0 ≈ sqrt(var(SR)) * ((1 - γ) Φ⁻¹(1 - 1/N) + γ Φ⁻¹(1 - 1/(N·e)))
    # where γ ≈ 0.5772 (Euler-Mascheroni)
    gamma_em = 0.5772156649
    if n_trials < 2:
        # No multiple-testing correction — DSR collapses to PSR (probabilistic SR).
        sr0 = 0.0
    else:
        term1 = (1.0 - gamma_em) * _norm_inv(1.0 - 1.0 / n_trials)
        term2 = gamma_em * _norm_inv(1.0 - 1.0 / (n_trials * math.e))
        sr0 = math.sqrt(var_sr) * (term1 + term2)

    z = (sr - sr0) / math.sqrt(var_sr)
    return _norm_cdf(z)


# ---------------------------------------------------------------------------
# Drawdown / Calmar
# ---------------------------------------------------------------------------

def max_drawdown(xs: Sequence[float]) -> float:
    """Maximum peak-to-trough drawdown of the cumulative compounded series.

    Returns a non-negative number — the maximum percentage decline from
    any peak to a subsequent trough. 0 for monotonically rising series.
    """
    if not xs:
        return 0.0
    nav = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in xs:
        nav *= 1.0 + r
        if nav > peak:
            peak = nav
        dd = (peak - nav) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def calmar(xs: Sequence[float], *, periods_per_year: int = TRADING_DAYS_PER_YEAR) -> float:
    """Calmar ratio = annualised return / max drawdown.

    Returns 0 when max drawdown is 0 (monotonically rising series — Calmar
    is undefined; we return 0 rather than infinity so downstream sorting is
    deterministic).
    """
    dd = max_drawdown(xs)
    if dd <= 0 or not xs:
        return 0.0
    cumulative = 1.0
    for r in xs:
        cumulative *= 1.0 + r
    annualised = cumulative ** (periods_per_year / len(xs)) - 1.0
    return annualised / dd


# ---------------------------------------------------------------------------
# Win rate / profit factor
# ---------------------------------------------------------------------------

def win_rate(xs: Sequence[float]) -> float:
    if not xs:
        return 0.0
    wins = sum(1 for x in xs if x > 0)
    return wins / len(xs)


def profit_factor(xs: Sequence[float]) -> float:
    """Sum of wins / |sum of losses|. Returns 0 if no losses (and wins>0)."""
    gross_wins = sum(x for x in xs if x > 0)
    gross_losses = -sum(x for x in xs if x < 0)
    if gross_losses == 0:
        return 0.0 if gross_wins == 0 else float("inf")
    return gross_wins / gross_losses


def avg_win(xs: Sequence[float]) -> float:
    wins = [x for x in xs if x > 0]
    return mean(wins) if wins else 0.0


def avg_loss(xs: Sequence[float]) -> float:
    losses = [x for x in xs if x < 0]
    return mean(losses) if losses else 0.0


# ---------------------------------------------------------------------------
# Bootstrap CI on Sharpe
# ---------------------------------------------------------------------------

def bootstrap_sharpe_ci(
    xs: Sequence[float],
    *,
    n_iter: int = 1000,
    block_size: int = 5,
    confidence: float = 0.95,
    seed: int | None = None,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> tuple[float, float]:
    """Block-bootstrap CI on the Sharpe ratio.

    Block size of 5 trading days preserves serial dependence (intra-week
    autocorrelation in returns). 1000 iterations is the spec's default.

    Returns (lower, upper) at the given confidence level.
    """
    n = len(xs)
    if n == 0 or block_size <= 0:
        return 0.0, 0.0
    rng = random.Random(seed)
    n_blocks_per_resample = max(1, n // block_size)

    sharpes: list[float] = []
    for _ in range(n_iter):
        resample: list[float] = []
        for _ in range(n_blocks_per_resample):
            start = rng.randint(0, max(0, n - block_size))
            resample.extend(xs[start:start + block_size])
        sharpes.append(sharpe(resample, periods_per_year=periods_per_year))

    sharpes.sort()
    alpha = (1.0 - confidence) / 2.0
    lo_idx = int(alpha * len(sharpes))
    hi_idx = int((1.0 - alpha) * len(sharpes)) - 1
    return sharpes[lo_idx], sharpes[max(hi_idx, lo_idx)]


# ---------------------------------------------------------------------------
# All-in-one
# ---------------------------------------------------------------------------

def compute_metrics(
    daily_returns: Sequence[float],
    *,
    n_trials: int = 1,
    bootstrap_iter: int = 1000,
    bootstrap_block_size: int = 5,
    seed: int | None = 42,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
) -> MetricsBundle:
    """Compute all headline metrics on a daily-return series."""
    sr = sharpe(daily_returns, periods_per_year=periods_per_year)
    lo, hi = bootstrap_sharpe_ci(
        daily_returns,
        n_iter=bootstrap_iter,
        block_size=bootstrap_block_size,
        seed=seed,
        periods_per_year=periods_per_year,
    )
    return MetricsBundle(
        n=len(daily_returns),
        mean=mean(daily_returns),
        median=median(daily_returns),
        std=stdev(daily_returns),
        skew=skew(daily_returns),
        kurtosis_excess=kurtosis_excess(daily_returns),
        sharpe=sr,
        deflated_sharpe=deflated_sharpe(
            daily_returns, n_trials=n_trials, periods_per_year=periods_per_year
        ),
        sharpe_ci_lower=lo,
        sharpe_ci_upper=hi,
        win_rate=win_rate(daily_returns),
        avg_win=avg_win(daily_returns),
        avg_loss=avg_loss(daily_returns),
        profit_factor=profit_factor(daily_returns),
        max_drawdown=max_drawdown(daily_returns),
        calmar=calmar(daily_returns, periods_per_year=periods_per_year),
    )


# ---------------------------------------------------------------------------
# Normal CDF + inverse (Beasley-Springer-Moro for inverse)
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# Beasley-Springer-Moro inverse normal CDF.
# Accuracy < 4.5e-4 absolute over the full range; sufficient for DSR.
_BSM_A = (
    -3.969683028665376e+01,
    2.209460984245205e+02,
    -2.759285104469687e+02,
    1.383577518672690e+02,
    -3.066479806614716e+01,
    2.506628277459239e+00,
)
_BSM_B = (
    -5.447609879822406e+01,
    1.615858368580409e+02,
    -1.556989798598866e+02,
    6.680131188771972e+01,
    -1.328068155288572e+01,
)
_BSM_C = (
    -7.784894002430293e-03,
    -3.223964580411365e-01,
    -2.400758277161838e+00,
    -2.549732539343734e+00,
    4.374664141464968e+00,
    2.938163982698783e+00,
)
_BSM_D = (
    7.784695709041462e-03,
    3.224671290700398e-01,
    2.445134137142996e+00,
    3.754408661907416e+00,
)
_BSM_LOW = 0.02425
_BSM_HIGH = 1.0 - _BSM_LOW


def _norm_inv(p: float) -> float:
    """Inverse standard-normal CDF via Beasley-Springer-Moro.

    Returns ``x`` such that ``Φ(x) == p``. Defined for ``p ∈ (0, 1)``;
    clamps to a finite value for edge cases so DSR is total.
    """
    if p <= 0.0:
        return -8.0
    if p >= 1.0:
        return 8.0
    if p < _BSM_LOW:
        q = math.sqrt(-2.0 * math.log(p))
        return (
            ((((_BSM_C[0] * q + _BSM_C[1]) * q + _BSM_C[2]) * q + _BSM_C[3]) * q + _BSM_C[4]) * q + _BSM_C[5]
        ) / ((((_BSM_D[0] * q + _BSM_D[1]) * q + _BSM_D[2]) * q + _BSM_D[3]) * q + 1.0)
    if p > _BSM_HIGH:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(
            ((((_BSM_C[0] * q + _BSM_C[1]) * q + _BSM_C[2]) * q + _BSM_C[3]) * q + _BSM_C[4]) * q + _BSM_C[5]
        ) / ((((_BSM_D[0] * q + _BSM_D[1]) * q + _BSM_D[2]) * q + _BSM_D[3]) * q + 1.0)
    q = p - 0.5
    r = q * q
    return (
        (((((_BSM_A[0] * r + _BSM_A[1]) * r + _BSM_A[2]) * r + _BSM_A[3]) * r + _BSM_A[4]) * r + _BSM_A[5]) * q
    ) / (((((_BSM_B[0] * r + _BSM_B[1]) * r + _BSM_B[2]) * r + _BSM_B[3]) * r + _BSM_B[4]) * r + 1.0)
