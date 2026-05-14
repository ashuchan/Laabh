"""Contextual Linear Thompson Sampling (LinTS).

Reward model: r = θ_a^T x + ε, where x is a 5-dim context vector.
Posterior: Bayesian linear regression with Gaussian prior.

Per-arm state stored in (A_inv, b, theta_hat):
    A = I/prior_var + X^T X   (Gram matrix, precision form)
    b = X^T r                 (response sum)
    theta_hat = A_inv @ b     (MAP estimate)

Sampling: draw θ̃ ~ N(theta_hat, A_inv), predict = θ̃^T x.
Select arm with max(predicted × |signal_strength|).

Context vector (5-dim, all normalized to [0, 1] or similar):
    0: vix_value / 30            clamped [0, 1]
    1: time_of_day_pct           fraction of session [0, 1]
    2: day_running_pnl_pct       clamped [-0.05, +0.05] → scaled to [0, 1]
    3: nifty_5d_return           clamped [-0.1, +0.1] → scaled to [0, 1]
    4: realized_vol_30min_pctile percentile over 30-day window [0, 1]
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TypeAlias

import numpy as np

ArmId: TypeAlias = str

# Backwards-compat default dimension. The 4-dim LLM-features extension lifts
# this to 9 at runtime when LAABH_LLM_MODE='feature' — passed in via
# LinTSSampler's ``context_dim`` constructor arg. Code paths that still call
# the unparameterised public helpers (e.g. tests, the deterministic backtest)
# keep getting the 5-dim shape and behaviour unchanged.
CONTEXT_DIM = 5
CONTEXT_DIM_WITH_LLM = 9

# Stable, ordered names for the 5-dim context vector. Surfaced via the
# bandit trace so the Decision Inspector can label dimensions without
# duplicating the schema. Keep in lockstep with ``build_context`` below.
_CONTEXT_DIM_NAMES: tuple[str, ...] = (
    "vix_norm",
    "tod_pct",
    "day_pnl_norm",
    "nifty_5d_norm",
    "rv30_pctile",
)

# LLM-extended dim names (Phase 3 cutover). Append-only — order MUST match
# the assembly in ``build_context_with_llm``.
_CONTEXT_DIM_NAMES_LLM: tuple[str, ...] = _CONTEXT_DIM_NAMES + (
    "llm_calibrated_conviction",
    "llm_thesis_durability",
    "llm_catalyst_specificity",
    "llm_risk_flag",
)


@dataclass
class LinTSArmState:
    """Per-arm Bayesian linear regression state."""

    a_inv: np.ndarray    # shape (CONTEXT_DIM, CONTEXT_DIM)
    b: np.ndarray        # shape (CONTEXT_DIM,)
    n_obs: int = 0

    @property
    def theta_hat(self) -> np.ndarray:
        return self.a_inv @ self.b

    def to_json(self) -> dict:
        return {
            "a_inv": self.a_inv.tolist(),
            "b": self.b.tolist(),
            "n_obs": self.n_obs,
        }

    @classmethod
    def from_json(cls, d: dict) -> "LinTSArmState":
        return cls(
            a_inv=np.array(d["a_inv"]),
            b=np.array(d["b"]),
            n_obs=d.get("n_obs", 0),
        )


class LinTSSampler:
    """Contextual Linear Thompson Sampling — same API as ThompsonSampler.

    ``context_dim`` defaults to 5 (legacy shape). Pass 9 to extend with the
    four LLM-feature dimensions; the sampler builds the cold-start state at
    the requested dimension and exposes ``context_dim`` for callers to
    validate persisted state on reload.
    """

    def __init__(
        self,
        arms: list[ArmId],
        rng: np.random.Generator,
        *,
        prior_var: float = 0.01,
        context_dim: int = CONTEXT_DIM,
    ) -> None:
        self._rng = rng
        self._prior_var = prior_var
        self._context_dim = context_dim
        self._states: dict[ArmId, LinTSArmState] = {
            arm: _cold_start(prior_var, dim=context_dim) for arm in arms
        }
        self._obs_var = prior_var

    @property
    def context_dim(self) -> int:
        return self._context_dim

    # ------------------------------------------------------------------
    # Core API (matches ThompsonSampler)
    # ------------------------------------------------------------------

    def select(
        self,
        signalling_arms: list[ArmId],
        *,
        context: np.ndarray,
        signal_strengths: dict[ArmId, float] | None = None,
        trace: dict | None = None,
        contexts: dict[ArmId, np.ndarray] | None = None,
    ) -> ArmId | None:
        """Pick arm by max(θ̃^T x × |signal_strength|).

        When ``trace`` is non-None, the method records the per-arm draws
        + scores so the Decision Inspector can render the tournament.
        Random sampling is unchanged — the captured draws are the same
        ones used for selection.

        Trace shape (when populated):
            {"algo": "lints",
             "context_vector": [...5 floats...],
             "context_dims": ["vix_norm", "tod_pct", "day_pnl_norm",
                              "nifty_5d_norm", "rv30_pctile"],
             "arms": {<arm_id>: {"posterior_mean":   <theta_hat·x>,
                                 "posterior_var":    <x^T A_inv x>,
                                 "sampled_mean":     <θ̃·x>,
                                 "signal_strength":  <|s|>,
                                 "score":            <pred × |s|>},
                      ...},
             "selected": <chosen_arm or None>,
             "n_competitors": <int>}
        """
        candidates = [a for a in signalling_arms if a in self._states]
        if not candidates:
            if trace is not None:
                trace["algo"] = "lints"
                trace["context_vector"] = context.tolist()
                trace["context_dims"] = list(self._dim_names())
                trace["arms"] = {}
                trace["selected"] = None
                trace["n_competitors"] = 0
            return None
        # Phase 3 (LAABH_LLM_MODE='feature') passes per-arm contexts so each
        # arm's prediction uses its own underlying's LLM features. When
        # ``contexts`` is None we fall back to the shared ``context`` for
        # all arms — backwards-compatible with every existing caller.
        shared_x = context
        scores: dict[ArmId, float] = {}
        per_arm: dict[ArmId, dict] = {}
        for arm in candidates:
            state = self._states[arm]
            x = contexts.get(arm, shared_x) if contexts is not None else shared_x
            theta_sample = self._rng.multivariate_normal(state.theta_hat, state.a_inv)
            pred = float(theta_sample @ x)
            strength = abs((signal_strengths or {}).get(arm, 1.0))
            score = pred * strength
            scores[arm] = score
            if trace is not None:
                per_arm[arm] = {
                    "posterior_mean": float(state.theta_hat @ x),
                    "posterior_var": float(x @ state.a_inv @ x),
                    "sampled_mean": pred,
                    "signal_strength": float(strength),
                    "score": float(score),
                    # When per-arm contexts differ from the shared one, surface
                    # the arm's actual x so the Decision Inspector can show it.
                    "context_vector": x.tolist() if contexts is not None else None,
                }
        chosen = max(scores, key=scores.__getitem__)
        if trace is not None:
            trace["algo"] = "lints"
            trace["context_vector"] = context.tolist()
            trace["context_dims"] = list(self._dim_names())
            trace["arms"] = per_arm
            trace["selected"] = chosen
            trace["n_competitors"] = len(candidates)
        return chosen

    def _dim_names(self) -> tuple[str, ...]:
        """Return the dim-name tuple matching this sampler's context dim."""
        if self._context_dim == CONTEXT_DIM_WITH_LLM:
            return _CONTEXT_DIM_NAMES_LLM
        return _CONTEXT_DIM_NAMES

    def update(self, arm: ArmId, reward: float, *, context: np.ndarray) -> None:
        """Sherman-Morrison rank-1 update of A_inv (avoids full matrix invert)."""
        if arm not in self._states:
            return
        state = self._states[arm]
        x = context
        # A_new = A_old + x x^T / obs_var
        # A_new_inv via Sherman-Morrison:
        #   u = A_inv @ x
        #   A_new_inv = A_inv - (u @ u^T) / (obs_var + x^T @ u)
        u = state.a_inv @ x
        denom = self._obs_var + float(x @ u)
        if abs(denom) < 1e-12:
            denom = 1e-12
        new_a_inv = state.a_inv - np.outer(u, u) / denom
        new_b = state.b + x * (reward / self._obs_var)
        self._states[arm] = LinTSArmState(a_inv=new_a_inv, b=new_b, n_obs=state.n_obs + 1)

    def snapshot(self) -> dict[ArmId, LinTSArmState]:
        return {arm: LinTSArmState(s.a_inv.copy(), s.b.copy(), s.n_obs) for arm, s in self._states.items()}

    def restore(self, snapshot: dict[ArmId, LinTSArmState]) -> None:
        self._states = {arm: LinTSArmState(s.a_inv.copy(), s.b.copy(), s.n_obs) for arm, s in snapshot.items()}

    def apply_forget(self, gamma: float) -> None:
        """Widen posterior (multiply A_inv by 1/γ — reduces precision)."""
        for arm, state in self._states.items():
            self._states[arm] = LinTSArmState(
                a_inv=state.a_inv / gamma,
                b=state.b,
                n_obs=state.n_obs,
            )

    def add_arm(self, arm: ArmId) -> None:
        if arm not in self._states:
            self._states[arm] = _cold_start(self._prior_var, dim=self._context_dim)

    def remove_arm(self, arm: ArmId) -> LinTSArmState | None:
        """Remove *arm* and return its state (for dormant pool). No-op if absent."""
        return self._states.pop(arm, None)

    def restore_arm(self, arm: ArmId, state: LinTSArmState) -> None:
        """Re-admit *arm* with a previously saved state (warm prior)."""
        self._states[arm] = LinTSArmState(
            a_inv=state.a_inv.copy(), b=state.b.copy(), n_obs=state.n_obs
        )

    def posterior_mean(self, arm: ArmId) -> float:
        if arm not in self._states:
            return 0.0
        return float(self._states[arm].theta_hat.mean())

    def posterior_var(self, arm: ArmId) -> float:
        if arm not in self._states:
            return self._prior_var
        return float(np.diag(self._states[arm].a_inv).mean())

    def posterior_var_for_context(self, arm: ArmId, context: np.ndarray) -> float:
        """Prediction variance ``x^T A_inv x`` for ``arm`` under ``context``.

        This is the *contextual* posterior variance — the right quantity to
        rank arms by exploration value when the bandit has been updated
        non-uniformly across context dimensions. ``posterior_var(arm)``
        above is a context-independent proxy (mean of A_inv's diagonal);
        use this when the caller already has the per-arm context.
        """
        state = self._states.get(arm)
        if state is None:
            return self._prior_var
        x = context
        return float(x @ state.a_inv @ x)

    def n_obs(self, arm: ArmId) -> int:
        """Return the count of observations folded into *arm*'s posterior."""
        state = self._states.get(arm)
        return state.n_obs if state is not None else 0

    def state_for_db(self, arm: ArmId) -> dict:
        """Serialise state to JSONB-safe dict."""
        if arm not in self._states:
            return {}
        return self._states[arm].to_json()

    def restore_from_db(self, arm: ArmId, d: dict) -> None:
        if d:
            self._states[arm] = LinTSArmState.from_json(d)


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------

def build_context(
    *,
    vix_value: float,
    time_of_day_pct: float,
    day_running_pnl_pct: float,
    nifty_5d_return: float,
    realized_vol_30min_pctile: float,
) -> np.ndarray:
    """Build and return the 5-dim normalised context vector."""
    return np.array([
        min(1.0, max(0.0, vix_value / 30.0)),
        min(1.0, max(0.0, time_of_day_pct)),
        (min(0.05, max(-0.05, day_running_pnl_pct)) + 0.05) / 0.10,
        (min(0.1, max(-0.1, nifty_5d_return)) + 0.1) / 0.20,
        min(1.0, max(0.0, realized_vol_30min_pctile)),
    ], dtype=float)


def _cold_start(prior_var: float, *, dim: int = CONTEXT_DIM) -> LinTSArmState:
    return LinTSArmState(
        a_inv=np.eye(dim) * prior_var,
        b=np.zeros(dim),
    )


def build_context_with_llm(
    *,
    vix_value: float,
    time_of_day_pct: float,
    day_running_pnl_pct: float,
    nifty_5d_return: float,
    realized_vol_30min_pctile: float,
    llm_calibrated_conviction: float,
    llm_thesis_durability: float,
    llm_catalyst_specificity: float,
    llm_risk_flag: float,
) -> np.ndarray:
    """9-dim context vector — Phase 3 LLM-feature extension.

    First five components mirror :func:`build_context` exactly so the
    deterministic features stay calibrated. LLM scores are appended in the
    order declared by ``_CONTEXT_DIM_NAMES_LLM``; they are NOT normalised
    further because the calibration step already produced bounded outputs
    (-3..+3 for calibrated_conviction; [0,1] / [-1,0] for the rest).
    """
    base = build_context(
        vix_value=vix_value,
        time_of_day_pct=time_of_day_pct,
        day_running_pnl_pct=day_running_pnl_pct,
        nifty_5d_return=nifty_5d_return,
        realized_vol_30min_pctile=realized_vol_30min_pctile,
    )
    llm_tail = np.array([
        float(llm_calibrated_conviction),
        float(llm_thesis_durability),
        float(llm_catalyst_specificity),
        float(llm_risk_flag),
    ], dtype=float)
    return np.concatenate([base, llm_tail])
