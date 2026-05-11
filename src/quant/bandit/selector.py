"""ArmSelector — top-level interface used by the orchestrator.

Wraps either ThompsonSampler or LinTSSampler depending on config and exposes
a uniform select() / update() / snapshot() / restore() API.

Restless-bandit support
-----------------------
``replace_arm(evict, admit)`` evicts a weak arm, saves its posterior to an
in-memory dormant pool keyed by arm_id, and admits a new arm — warm (from
dormant pool) or cold (uniform prior). The dormant pool is session-scoped
and persists across replacement cycles so previously-seen arms re-enter with
their accumulated learning intact.
"""
from __future__ import annotations

from typing import TypeAlias, Union

import numpy as np

from src.quant.bandit.posterior import PosteriorState  # noqa: F401 — used in _dormant type
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
        # Dormant pool: arm_id → saved posterior state from a previous eviction.
        # Arms re-admitted from here get warm priors instead of cold start.
        # Type is LinTSArmState when algo="lints", PosteriorState when algo="thompson".
        # NOTE: the dormant pool is intentionally excluded from snapshot()/restore().
        # snapshot() is used for crash-recovery replay of today's active arms only;
        # dormant arms are session-scoped and start cold on a process restart.
        self._dormant: dict[ArmId, Union[LinTSArmState, "PosteriorState"]] = {}

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
        trace: dict | None = None,
    ) -> ArmId | None:
        """Pick best arm from signalling candidates.

        ``trace`` (optional) is forwarded to the underlying impl for the
        Decision Inspector. See ``LinTSSampler.select`` /
        ``ThompsonSampler.select`` for the per-algo trace shape.
        """
        if self._algo == "lints":
            ctx = context if context is not None else np.zeros(5)
            return self._impl.select(
                signalling_arms,
                context=ctx,
                signal_strengths=signal_strengths,
                trace=trace,
            )
        # Phase-5 reverted: cross-primitive strength weighting in Thompson is
        # mathematically incoherent — different primitives compute strength
        # in incomparable units (vwap_revert: σ-distance; vol_breakout: BB
        # expansion ratio; momentum: vol-normalised return). Multiplying
        # samples by these and comparing across arms biases selection
        # toward whichever primitive produces the highest *numbers*, not
        # the highest *expected return*. Live Phase-5 backtest confirmed
        # the bias hurt: take_profit exits dropped to 0/run, win-rate
        # halved. We keep ``ThompsonSampler.select``'s ``signal_strengths``
        # parameter for direct experimentation, but the production path
        # via ``ArmSelector`` no longer forwards it. LinTS keeps the
        # weighting because its context vector at least scopes learning.
        return self._impl.select(signalling_arms, trace=trace)

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

    def n_obs(self, arm: ArmId) -> int:
        """Return the per-arm observation count from the active impl."""
        return self._impl.n_obs(arm)

    def snapshot(self):
        """Snapshot active arm posteriors. The dormant pool is NOT included —
        it is session-scoped and does not need crash-recovery replay."""
        return self._impl.snapshot()

    def restore(self, snapshot) -> None:
        """Restore active arm posteriors from *snapshot*. Dormant pool unchanged."""
        self._impl.restore(snapshot)

    # ------------------------------------------------------------------
    # Restless-bandit arm replacement (intraday universe expansion)
    # ------------------------------------------------------------------

    def evict_arm(self, arm: ArmId) -> None:
        """Remove *arm* from the active set and save its state to the dormant pool.

        Safe to call even if the arm is not currently active (no-op).
        """
        saved = self._impl.remove_arm(arm)
        if saved is not None:
            self._dormant[arm] = saved

    def admit_arm(self, arm: ArmId) -> bool:
        """Add *arm* to the active set.

        If *arm* is in the dormant pool (previously seen this session) it is
        re-admitted with its warm posterior. Otherwise it starts cold at the
        prior. Returns True when admitted from dormant pool (warm), False when
        cold-started.
        """
        warm_state = self._dormant.pop(arm, None)
        if warm_state is not None:
            self._impl.restore_arm(arm, warm_state)
            return True
        # Cold start
        self.add_arm(arm)
        return False

    def replace_arm(self, evict: ArmId, admit: ArmId) -> bool:
        """Atomically evict one arm and admit another.

        Returns True when the admitted arm had warm priors from the dormant
        pool, False when it started cold.
        """
        self.evict_arm(evict)
        return self.admit_arm(admit)

    @property
    def dormant_arm_ids(self) -> list[ArmId]:
        """Return the IDs currently in the dormant pool."""
        return list(self._dormant.keys())
