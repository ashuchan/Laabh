"""Daily tier refresh — computes which F&O instruments belong to Tier 1 vs Tier 2.

Runs at 06:00 IST after the Phase 1 universe filter.

Tier 1 (~35): 5 index underlyings + top-N equities by 5-day average option volume.
Tier 2 (~170): remaining F&O-eligible instruments.

Writes to fno_collection_tiers (upsert — idempotent within the same day).
"""
from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import func, select, text

from src.config import get_settings
from src.db import session_scope
from src.models.fno_chain import OptionsChain
from src.models.fno_collection_tier import FNOCollectionTier
from src.models.instrument import Instrument

# Indices are always Tier 1 regardless of volume
_INDEX_SYMBOLS = frozenset(
    {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
)

_settings = get_settings()


async def refresh() -> dict[str, int]:
    """Recompute tier assignments for all active F&O instruments.

    Returns a dict with counts: {'tier1': N, 'tier2': M}.
    """
    now = datetime.now(tz=timezone.utc)
    tier1_size = _settings.fno_tier1_size

    async with session_scope() as session:
        # --- Load all active F&O instruments ---
        result = await session.execute(
            select(Instrument).where(
                Instrument.is_fno == True,  # noqa: E712
                Instrument.is_active == True,  # noqa: E712
            )
        )
        instruments: list[Instrument] = result.scalars().all()

    if not instruments:
        logger.warning("tier_manager: no active F&O instruments found")
        return {"tier1": 0, "tier2": 0}

    # --- Compute 5-day average option volume from options_chain ---
    async with session_scope() as session:
        vol_result = await session.execute(
            select(
                OptionsChain.instrument_id,
                func.avg(OptionsChain.volume).label("avg_vol"),
            )
            .where(
                OptionsChain.snapshot_at >= text("NOW() - INTERVAL '5 days'"),
                OptionsChain.volume.isnot(None),
            )
            .group_by(OptionsChain.instrument_id)
        )
        volume_map: dict[object, float] = {
            row.instrument_id: float(row.avg_vol or 0)
            for row in vol_result
        }

    # --- Classify instruments ---
    indices = [i for i in instruments if i.symbol.upper() in _INDEX_SYMBOLS]
    equities = [i for i in instruments if i.symbol.upper() not in _INDEX_SYMBOLS]

    # Sort equities by 5d avg volume descending
    equities_sorted = sorted(
        equities,
        key=lambda i: volume_map.get(i.id, 0.0),
        reverse=True,
    )

    # Top slots for equities after reserving index slots
    equity_tier1_slots = max(0, tier1_size - len(indices))
    tier1_equity = equities_sorted[:equity_tier1_slots]
    tier2_equity = equities_sorted[equity_tier1_slots:]

    tier1_ids = {i.id for i in indices} | {i.id for i in tier1_equity}

    # --- Upsert into fno_collection_tiers ---
    async with session_scope() as session:
        for inst in instruments:
            tier_val = 1 if inst.id in tier1_ids else 2
            avg_vol = int(volume_map.get(inst.id, 0))

            existing = await session.get(FNOCollectionTier, inst.id)
            if existing is None:
                session.add(
                    FNOCollectionTier(
                        instrument_id=inst.id,
                        tier=tier_val,
                        avg_volume_5d=avg_vol,
                        last_promoted_at=now if tier_val == 1 else None,
                        updated_at=now,
                    )
                )
            else:
                prev_tier = existing.tier
                existing.tier = tier_val
                existing.avg_volume_5d = avg_vol
                existing.updated_at = now
                if tier_val == 1 and prev_tier != 1:
                    existing.last_promoted_at = now

    tier1_count = len(tier1_ids)
    tier2_count = len(instruments) - tier1_count
    logger.info(
        f"tier_manager: refresh complete — tier1={tier1_count}, tier2={tier2_count}"
    )
    return {"tier1": tier1_count, "tier2": tier2_count}
