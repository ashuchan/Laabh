"""Normal-Gaussian conjugate posterior state for each bandit arm.

Murphy (2007) §3.3: Gaussian likelihood with Gaussian prior on the mean.
Prior:   μ ~ N(m_0, s_0²)
Update:  μ | r ~ N(m_n, s_n²)
    s_n² = 1 / (1/s_(n-1)² + 1/σ²)   [precision update]
    m_n  = s_n² × (m_(n-1)/s_(n-1)² + r/σ²)

We assume the observation variance σ² is fixed (= prior_var). This makes
the update closed-form and avoids estimating a second parameter with few
observations.

Forgetting factor γ: applied once per day at session start by multiplying
the precision (1/s²) by γ, which *increases* s² (widens the posterior),
letting tomorrow's data correct yesterday's beliefs.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PosteriorState:
    """Sufficient statistics for one arm's Normal posterior."""

    mean: float         # m_n
    var: float          # s_n²  (posterior variance, not observation variance)
    n_obs: int = 0

    def update(self, reward: float, *, obs_var: float) -> "PosteriorState":
        """Return a new posterior after observing *reward*."""
        new_precision = 1.0 / self.var + 1.0 / obs_var
        new_var = 1.0 / new_precision
        new_mean = new_var * (self.mean / self.var + reward / obs_var)
        return PosteriorState(mean=new_mean, var=new_var, n_obs=self.n_obs + 1)

    def apply_forget(self, gamma: float) -> "PosteriorState":
        """Return a new posterior with precision shrunk by γ (var widened by 1/γ).

        Keeps mean unchanged; only uncertainty grows.
        """
        return PosteriorState(
            mean=self.mean,
            var=self.var / gamma,
            n_obs=self.n_obs,
        )

    def sample(self, rng) -> float:
        """Draw a single sample from the posterior distribution."""
        import math
        return rng.normal(self.mean, math.sqrt(self.var))
