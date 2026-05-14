"""Replay today's chain snapshots through the fixed live feature_store + primitives.

End-to-end validation that the SQL fix unblocks the quant pipeline. Reads the
universe the quant orchestrator actually used today from quant_day_state, then
walks the 09:18 → 14:30 IST session at 3-min intervals. At each tick:

  1. Calls feature_store.get(uid, ts) — exercises the fixed live SQL.
  2. Feeds the bundle + per-symbol history into every enabled primitive.
  3. Counts signals whose |strength| clears LAABH_QUANT_MIN_SIGNAL_STRENGTH —
     these are the ones that would have reached the bandit selector.

The bandit + sizer + recorder paths are intentionally *not* replayed: their
posteriors are stateful and depend on the live tick stream. A non-zero count of
"signals passing the strength gate" is sufficient evidence that the fix
restores the entry-decision pipeline.

Run:
    python scripts/validate_feature_store_replay.py            # today
    python scripts/validate_feature_store_replay.py 2026-05-14 # specific date
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Project root on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytz
from loguru import logger
from sqlalchemy import text

logger.remove()
logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level} | {message}")

from src.config import get_settings
from src.db import dispose_engine, session_scope
from src.quant import feature_store
from src.quant.orchestrator import _load_primitives

_IST = pytz.timezone("Asia/Kolkata")


async def replay(d: date) -> None:
    settings = get_settings()
    min_strength = settings.laabh_quant_min_signal_strength
    primitives_list = settings.quant_primitives_list

    async with session_scope() as session:
        row = (await session.execute(
            text("SELECT universe FROM quant_day_state WHERE date = :d"),
            {"d": d},
        )).one_or_none()

    if row is None:
        logger.error(f"No quant_day_state row for {d} — replay aborted")
        return

    universe_uids: dict[str, uuid.UUID] = {
        u["symbol"]: uuid.UUID(u["id"]) for u in row.universe
    }
    logger.info(
        f"Replaying {d}: universe={len(universe_uids)} symbols, "
        f"primitives={primitives_list}, min_strength={min_strength}"
    )

    # Verify the SQL works before walking the session.
    await feature_store.ensure_schema()
    logger.info("ensure_schema passed — SQL probes valid against live schema")

    primitives = _load_primitives(primitives_list)
    max_history = max((p.warmup_bars for p in primitives), default=10) + 2

    start_ist = _IST.localize(datetime.combine(
        d, datetime.min.time().replace(hour=9, minute=18)
    ))
    end_ist = _IST.localize(datetime.combine(
        d, datetime.min.time().replace(hour=14, minute=30)
    ))
    start_utc = start_ist.astimezone(timezone.utc)
    end_utc = end_ist.astimezone(timezone.utc)
    step = timedelta(minutes=3)

    history: dict[str, list] = {sym: [] for sym in universe_uids}
    counters = {
        "ticks": 0,
        "bundles_built": 0,
        "bundles_none": 0,
        "fetch_errors": 0,
        "signals_total": 0,
        "signals_passed_gate": 0,
    }
    by_primitive: dict[str, int] = {p.name: 0 for p in primitives}
    passing_signals: list[tuple] = []

    current = start_utc
    while current <= end_utc:
        counters["ticks"] += 1
        for sym, uid in universe_uids.items():
            try:
                bundle = await feature_store.get(uid, current)
            except Exception as exc:
                counters["fetch_errors"] += 1
                logger.warning(f"{sym}@{current.astimezone(_IST):%H:%M}: {exc!r}")
                continue
            if bundle is None:
                counters["bundles_none"] += 1
                continue
            counters["bundles_built"] += 1
            hist = history[sym]
            hist.append(bundle)
            if len(hist) > max_history:
                del hist[0]
            for prim in primitives:
                sig = prim.compute_signal(bundle, hist[:-1])
                if sig is None:
                    continue
                counters["signals_total"] += 1
                by_primitive[prim.name] += 1
                if abs(sig.strength) >= min_strength:
                    counters["signals_passed_gate"] += 1
                    passing_signals.append((
                        sym, prim.name, sig.direction, float(sig.strength),
                        current.astimezone(_IST),
                    ))
        current += step

    passing_signals.sort(key=lambda r: abs(r[3]), reverse=True)

    _print_report(d, len(universe_uids), counters, by_primitive, passing_signals)
    await dispose_engine()


def _print_report(
    d: date,
    universe_size: int,
    counters: dict,
    by_primitive: dict[str, int],
    passing: list[tuple],
) -> None:
    print()
    print("=" * 78)
    print(f"FEATURE STORE REPLAY VALIDATION — {d}")
    print("=" * 78)
    print(f"Universe size                  : {universe_size}")
    print(f"Ticks walked (3-min cadence)   : {counters['ticks']}")
    print(f"Bundles built                  : {counters['bundles_built']}")
    print(f"Bundles None (stale / no data) : {counters['bundles_none']}")
    print(f"Per-fetch exceptions           : {counters['fetch_errors']}")
    print("-" * 78)
    print(f"Total raw primitive signals    : {counters['signals_total']}")
    for name, n in by_primitive.items():
        print(f"  {name:<14}: {n}")
    print(f"Signals >= strength gate        : {counters['signals_passed_gate']}")
    print("-" * 78)
    if passing:
        print(f"\nTop {min(15, len(passing))} signals that would have reached the bandit:")
        print(f"{'symbol':<12} {'primitive':<14} {'dir':<9} {'strength':>10}  time IST")
        print("-" * 65)
        for sym, prim, direction, strength, ts in passing[:15]:
            print(
                f"{sym:<12} {prim:<14} {direction:<9} "
                f"{strength:>+10.4f}  {ts:%H:%M}"
            )
    else:
        print("\nNo signals cleared the strength gate.")
    print()
    if counters["fetch_errors"] > 0:
        print(
            "WARNING: some feature fetches raised — investigate the warnings "
            "above before declaring the fix complete."
        )
    elif counters["bundles_built"] == 0:
        print(
            "WARNING: zero bundles built. The fix is wired but the chain "
            "snapshots may be missing for the requested date."
        )
    elif counters["signals_passed_gate"] == 0:
        print(
            "INFO: bundles built but no signal cleared the strength gate. "
            "The pipeline is unblocked; primitives just didn't fire today."
        )
    else:
        print(
            "OK — bundles built, primitives fired, and signals cleared the "
            "strength gate. The quant loop would have reached the bandit "
            "selector with the fixed feature_store."
        )
    print()


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    target = date.fromisoformat(arg) if arg else date.today()
    asyncio.run(replay(target))
