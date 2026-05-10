"""Vanilla Thompson Sampling allocator.

Each arm maintains a Normal-Gaussian conjugate posterior over expected
per-trade return. At each selection the sampler draws one sample per
candidate arm and returns the arm with the highest sample.
"""
from __future__ import annotations

import time
from typing import TypeAlias

import numpy as np

from src.quant.bandit.posterior import PosteriorState

ArmId: TypeAlias = str


class ThompsonSampler:
    """Thompson Sampling over a fixed set of arms."""

    def __init__(
        self,
        arms: list[ArmId],
        rng: np.random.Generator,
        *,
        prior_mean: float = 0.0,
        prior_var: float = 0.01,
        obs_var: float | None = None,
    ) -> None:
        """Initialise posteriors for all arms at the prior.

        Args:
            arms: All possible arm IDs in this session.
            rng: Pre-seeded numpy Generator (caller owns the seed).
            prior_mean: μ_0 — prior belief about expected per-trade return.
            prior_var: s_0² — prior variance (uncertainty).
            obs_var: Observation noise variance. Defaults to prior_var.
        """
        self._rng = rng
        self._prior_var = prior_var
        self._obs_var = obs_var if obs_var is not None else prior_var
        self._posteriors: dict[ArmId, PosteriorState] = {
            arm: PosteriorState(mean=prior_mean, var=prior_var) for arm in arms
        }

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def select(
        self,
        signalling_arms: list[ArmId],
        *,
        signal_strengths: dict[ArmId, float] | None = None,
        trace: dict | None = None,
    ) -> ArmId | None:
        """Pick the best arm from those currently signalling.

        Returns None if *signalling_arms* is empty or none match known arms.

        ``signal_strengths`` (Phase-5 fix): per-arm primitive signal
        strength, used to *weight* the sampled posterior — score is
        ``sampled_mean × |signal_strength|``. Without it (or when None),
        every arm gets weight 1.0 and selection is pure-Thompson — bit-for-
        bit equivalent to the prior behaviour. With it, "stronger" signals
        get bandit preference, matching how LinTS already weighted them.

        When ``trace`` is non-None, the method populates it with the
        full per-arm tournament so the Decision Inspector can render the
        bandit card. Random sampling is unchanged — every arm's sample is
        captured exactly as it was used to score, so the trace is faithful.

        Trace shape (when populated):
            {"algo": "thompson",
             "arms": {<arm_id>: {"posterior_mean": ...,
                                 "posterior_var": ...,
                                 "sampled_mean": ...,
                                 "signal_strength": ...,
                                 "score": <sampled_mean × |strength|>},
                      ...},
             "selected": <chosen_arm or None>,
             "n_competitors": <int>}
        """
        candidates = [a for a in signalling_arms if a in self._posteriors]
        if not candidates:
            if trace is not None:
                trace["algo"] = "thompson"
                trace["arms"] = {}
                trace["selected"] = None
                trace["n_competitors"] = 0
            return None
        strengths = signal_strengths or {}
        samples = {arm: self._posteriors[arm].sample(self._rng) for arm in candidates}
        # Weight sample by |strength| — primitives carefully calibrate
        # strength (saturating tanh, bounded [-1, 1]); the bandit ought
        # to use that signal rather than ignoring it. Defaults to 1.0
        # when an arm has no entry in the dict, preserving cold-start
        # behaviour for new arms.
        scores = {
            arm: samples[arm] * abs(strengths.get(arm, 1.0))
            for arm in candidates
        }
        chosen = max(scores, key=scores.__getitem__)
        if trace is not None:
            trace["algo"] = "thompson"
            trace["arms"] = {
                arm: {
                    "posterior_mean": float(self._posteriors[arm].mean),
                    "posterior_var": float(self._posteriors[arm].var),
                    "sampled_mean": float(samples[arm]),
                    "signal_strength": float(abs(strengths.get(arm, 1.0))),
                    "score": float(scores[arm]),
                }
                for arm in candidates
            }
            trace["selected"] = chosen
            trace["n_competitors"] = len(candidates)
        return chosen

    def update(self, arm: ArmId, reward: float) -> None:
        """Update the posterior for *arm* with observed *reward*."""
        if arm not in self._posteriors:
            return
        self._posteriors[arm] = self._posteriors[arm].update(
            reward, obs_var=self._obs_var
        )

    def snapshot(self) -> dict[ArmId, PosteriorState]:
        """Return a shallow copy of all arm posteriors."""
        return {arm: PosteriorState(p.mean, p.var, p.n_obs) for arm, p in self._posteriors.items()}

    def restore(self, snapshot: dict[ArmId, PosteriorState]) -> None:
        """Replace internal posteriors with *snapshot* (exact state)."""
        self._posteriors = {
            arm: PosteriorState(p.mean, p.var, p.n_obs) for arm, p in snapshot.items()
        }

    def apply_forget(self, gamma: float) -> None:
        """Apply forgetting factor γ to all arm posteriors (call at day-start)."""
        self._posteriors = {
            arm: p.apply_forget(gamma) for arm, p in self._posteriors.items()
        }

    def add_arm(self, arm: ArmId, *, prior_mean: float = 0.0) -> None:
        """Register a new arm (cold-start at prior)."""
        if arm not in self._posteriors:
            self._posteriors[arm] = PosteriorState(mean=prior_mean, var=self._prior_var)

    def posterior_mean(self, arm: ArmId) -> float:
        """Return the current posterior mean for *arm* (0.0 if unknown)."""
        return self._posteriors[arm].mean if arm in self._posteriors else 0.0

    def posterior_var(self, arm: ArmId) -> float:
        """Return the current posterior variance for *arm*."""
        return self._posteriors[arm].var if arm in self._posteriors else self._prior_var

    def n_obs(self, arm: ArmId) -> int:
        """Return the count of observations folded into *arm*'s posterior."""
        post = self._posteriors.get(arm)
        return post.n_obs if post is not None else 0
