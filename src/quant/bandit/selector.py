"""ArmSelector — top-level interface used by the orchestrator.

Wraps either ThompsonSampler or LinTSSampler depending on config and exposes
a uniform select() / update() / snapshot() / restore() API.
"""
from __future__ import annotations

from typing import TypeAlias

import numpy as np

from src.quant.bandit.posterior import PosteriorState
from src.quant.bandit.thompson import ThompsonSampler
from src.quant.bandit.lints import LinTSSampler, LinTSArmState, build_context

ArmId: TypeAlias = str


class ArmSelector:
    """Unified bandit selector — Thompson or LinTS, same call-site API."""

    def __init__(
        self,
        arms: list[ArmId],
        *,
        algo: str = "thompson",
        prior_mean: float = 0.0,
        prior_var: float = 0.01,
        seed: int | None = None,
    ) -> None:
        rng_seed = seed if seed is not None else np.random.SeedSequence().entropy
        self._rng = np.random.default_rng(rng_seed)
        self._algo = algo
        self._prior_mean = prior_mean
        self._prior_var = prior_var

        if algo == "lints":
            self._impl = LinTSSampler(arms, self._rng, prior_var=prior_var)
        else:
            self._impl = ThompsonSampler(
                arms, self._rng, prior_mean=prior_mean, prior_var=prior_var
            )

    def select(
        self,
        signalling_arms: list[ArmId],
        *,
        context: np.ndarray | None = None,
        signal_strengths: dict[ArmId, float] | None = None,
    ) -> ArmId | None:
        """Pick best arm from signalling candidates."""
        if self._algo == "lints":
            ctx = context if context is not None else np.zeros(5)
            return self._impl.select(signalling_arms, context=ctx, signal_strengths=signal_strengths)
        return self._impl.select(signalling_arms)

    def update(
        self,
        arm: ArmId,
        reward: float,
        *,
        context: np.ndarray | None = None,
    ) -> None:
        if self._algo == "lints":
            ctx = context if context is not None else np.zeros(5)
            self._impl.update(arm, reward, context=ctx)
        else:
            self._impl.update(arm, reward)

    def apply_forget(self, gamma: float) -> None:
        self._impl.apply_forget(gamma)

    def add_arm(self, arm: ArmId) -> None:
        if self._algo == "lints":
            self._impl.add_arm(arm)
        else:
            self._impl.add_arm(arm, prior_mean=self._prior_mean)

    def posterior_mean(self, arm: ArmId) -> float:
        return self._impl.posterior_mean(arm)

    def posterior_var(self, arm: ArmId) -> float:
        return self._impl.posterior_var(arm)

    def snapshot(self):
        return self._impl.snapshot()

    def restore(self, snapshot) -> None:
        self._impl.restore(snapshot)
