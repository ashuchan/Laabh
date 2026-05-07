"""Layer 6 — Posterior persistence with forgetting factor.

save_eod(): write all arm posteriors to bandit_arm_state at day-end.
load_morning(): read most-recent rows, apply γ-decay, return an ArmSelector.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import select, text

from src.config import get_settings
from src.db import session_scope
from src.quant.bandit.selector import ArmSelector

if TYPE_CHECKING:
    pass


async def save_eod(
    portfolio_id: uuid.UUID,
    trading_date: date,
    selector: ArmSelector,
    arms: list[str],
    underlying_map: dict[str, uuid.UUID],
    *,
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> None:
    """Persist all arm posteriors to bandit_arm_state for *trading_date*.

    Args:
        portfolio_id: Portfolio UUID.
        trading_date: The trading day being saved.
        selector: The ArmSelector instance with current posteriors.
        arms: All arm IDs in scope (format: "{symbol}_{primitive_name}").
        underlying_map: Maps symbol → underlying_id UUID.
    """
    from src.models.bandit_arm_state import BanditArmState

    settings = get_settings()
    async with session_scope() as session:
        for arm_id in arms:
            symbol, primitive_name = _split_arm(arm_id)
            underlying_id = underlying_map.get(symbol)
            if underlying_id is None:
                continue

            mean = selector.posterior_mean(arm_id)
            var = selector.posterior_var(arm_id)

            # Upsert (insert or update)
            existing = await session.get(
                BanditArmState,
                (portfolio_id, underlying_id, primitive_name, trading_date),
            )
            if existing is None:
                state = BanditArmState(
                    portfolio_id=portfolio_id,
                    underlying_id=underlying_id,
                    primitive_name=primitive_name,
                    date=trading_date,
                    posterior_mean=mean,
                    posterior_var=var,
                    n_observations=0,
                )
                session.add(state)
            else:
                existing.posterior_mean = mean
                existing.posterior_var = var
                existing.last_updated_at = datetime.now(timezone.utc)

            # LinTS extras
            if settings.laabh_quant_bandit_algo == "lints":
                lints_impl = getattr(selector, "_impl", None)
                if lints_impl and hasattr(lints_impl, "state_for_db"):
                    d = lints_impl.state_for_db(arm_id)
                    target = existing if existing else state
                    target.theta = d.get("b")
                    target.a_inv = d.get("a_inv")
                    target.b_vector = d.get("b")

    logger.info(f"save_eod: wrote {len(arms)} arm posteriors for {trading_date}")


async def load_morning(
    portfolio_id: uuid.UUID,
    trading_date: date,
    arms: list[str],
    underlying_map: dict[str, uuid.UUID],
    *,
    as_of: datetime | None = None,
    dryrun_run_id: uuid.UUID | None = None,
) -> ArmSelector:
    """Load bandit state from DB and apply forgetting factor.

    Returns an initialised ArmSelector with posteriors pre-loaded.
    If no row exists for an arm, it starts at the config prior.
    """
    from src.models.bandit_arm_state import BanditArmState

    settings = get_settings()
    gamma = settings.laabh_quant_bandit_forget_factor

    selector = ArmSelector(
        arms,
        algo=settings.laabh_quant_bandit_algo,
        prior_mean=settings.laabh_quant_bandit_prior_mean,
        prior_var=settings.laabh_quant_bandit_prior_var,
        seed=settings.laabh_quant_bandit_seed,
    )

    async with session_scope() as session:
        for arm_id in arms:
            symbol, primitive_name = _split_arm(arm_id)
            underlying_id = underlying_map.get(symbol)
            if underlying_id is None:
                continue

            # Most-recent row for this arm (any prior date)
            q = (
                select(BanditArmState)
                .where(BanditArmState.portfolio_id == portfolio_id)
                .where(BanditArmState.underlying_id == underlying_id)
                .where(BanditArmState.primitive_name == primitive_name)
                .where(BanditArmState.date < trading_date)
                .order_by(BanditArmState.date.desc())
                .limit(1)
            )
            row = (await session.execute(q)).scalar_one_or_none()
            if row is None:
                continue

            # Apply γ-decay: widen variance (reduce precision)
            decayed_var = float(row.posterior_var or settings.laabh_quant_bandit_prior_var) / gamma
            decayed_mean = float(row.posterior_mean or settings.laabh_quant_bandit_prior_mean)

            # Patch the selector's internal posterior
            _patch_posterior(selector, arm_id, mean=decayed_mean, var=decayed_var)

    logger.info(f"load_morning: loaded posteriors for {trading_date} with γ={gamma}")
    return selector


def _split_arm(arm_id: str) -> tuple[str, str]:
    """Split "SYMBOL_primitive" into (symbol, primitive_name).

    Handles symbols with underscores by splitting on the last underscore token
    that matches a known primitive name.
    """
    known_primitives = {"orb", "vwap_revert", "ofi", "vol_breakout", "momentum", "index_revert"}
    for prim in sorted(known_primitives, key=len, reverse=True):
        suffix = f"_{prim}"
        if arm_id.endswith(suffix):
            symbol = arm_id[: -len(suffix)]
            return symbol, prim
    # Fallback: split on last underscore
    parts = arm_id.rsplit("_", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (arm_id, "unknown")


def _patch_posterior(selector: ArmSelector, arm_id: str, *, mean: float, var: float) -> None:
    """Directly overwrite the in-memory posterior for one arm."""
    from src.quant.bandit.posterior import PosteriorState

    impl = selector._impl
    if hasattr(impl, "_posteriors"):
        # ThompsonSampler
        if arm_id in impl._posteriors:
            old = impl._posteriors[arm_id]
            impl._posteriors[arm_id] = PosteriorState(mean=mean, var=var, n_obs=old.n_obs)
    elif hasattr(impl, "_states"):
        # LinTSSampler — only patch the per-arm mean scalar; full state is in JSONB
        pass
