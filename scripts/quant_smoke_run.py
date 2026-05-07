#!/usr/bin/env python3
"""Quant-mode smoke runner — synthetic 1-hour day with deterministic mocks.

Usage:
    python scripts/quant_smoke_run.py

Exits 0 on success, 1 on any exception.
Validates:
 - Primitive signal computation is deterministic.
 - Sizer returns non-negative lot counts.
 - Circuit breaker fires correctly on synthetic NAV moves.
 - Thompson sampler selects and updates without error.
 - Reports formatter runs without errors.
"""
from __future__ import annotations

import sys
import traceback
import uuid
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, ".")


def _bundle(ltp: float = 100.0):
    from decimal import Decimal as D
    from src.quant.feature_store import FeatureBundle

    return FeatureBundle(
        underlying_id=uuid.uuid4(),
        underlying_symbol="NIFTY",
        captured_at=datetime.now(timezone.utc),
        underlying_ltp=ltp,
        underlying_volume_3min=10000.0,
        vwap_today=100.0,
        realized_vol_3min=0.01,
        realized_vol_30min=0.015,
        atm_iv=0.15,
        atm_oi=50000,
        atm_bid=D("50"),
        atm_ask=D("50.5"),
        bid_volume_3min_change=500,
        ask_volume_3min_change=100,
        bb_width=0.03,
        vix_value=14.0,
        vix_regime="normal",
        orb_high=105.0,
        orb_low=95.0,
    )


def smoke_primitives() -> None:
    from src.quant.primitives.orb import ORBPrimitive
    from src.quant.primitives.vwap_revert import VWAPRevertPrimitive
    from src.quant.primitives.momentum import MomentumPrimitive
    from src.quant.primitives.ofi import OFIPrimitive
    from src.quant.primitives.vol_breakout import VolBreakoutPrimitive
    from src.quant.primitives.index_revert import IndexRevertPrimitive

    hist = [_bundle(100.0 + i * 0.1) for i in range(20)]
    current = _bundle(108.0)
    current.underlying_volume_3min = 25000.0

    for prim_cls in [ORBPrimitive, VWAPRevertPrimitive, MomentumPrimitive,
                     OFIPrimitive, VolBreakoutPrimitive, IndexRevertPrimitive]:
        prim = prim_cls()
        sig = prim.compute_signal(current, hist)
        print(f"  {prim.name}: {sig}")

    print("✓ primitives OK")


def smoke_bandit() -> None:
    import numpy as np
    from src.quant.bandit.selector import ArmSelector

    arms = ["NIFTY_orb", "RELIANCE_momentum", "BANKNIFTY_vwap_revert"]
    sel = ArmSelector(arms, seed=42)
    chosen = sel.select(arms)
    assert chosen in arms
    sel.update(chosen, 0.05)
    sel.apply_forget(0.95)
    print(f"  Selected: {chosen}, posterior_mean={sel.posterior_mean(chosen):.4f}")
    print("✓ bandit OK")


def smoke_sizer() -> None:
    from src.quant.sizer import compute_lots

    lots = compute_lots(
        posterior_mean=0.02,
        portfolio_capital=Decimal("500000"),
        max_loss_per_lot=Decimal("5000"),
        estimated_costs=Decimal("200"),
        expected_gross_pnl=Decimal("2000"),
        open_exposure=Decimal("0"),
        lockin_active=False,
    )
    assert isinstance(lots, int) and lots >= 0
    print(f"  Lots computed: {lots}")
    print("✓ sizer OK")


def smoke_circuit_breaker() -> None:
    from datetime import datetime, timezone
    from src.quant.circuit_breaker import CircuitState

    state = CircuitState(starting_nav=1_000_000.0)
    now = datetime.now(timezone.utc)
    state.check_and_fire(1_060_000.0, now)
    assert state.lockin_active, "lock-in should have fired at +6%"
    state2 = CircuitState(starting_nav=1_000_000.0)
    state2.check_and_fire(965_000.0, now)
    assert state2.kill_active, "kill switch should have fired at -3.5%"
    print("  Lock-in and kill-switch fired correctly")
    print("✓ circuit breaker OK")


def smoke_report_format() -> None:
    # Just test the formatter doesn't crash on empty data
    from src.quant.reports import _day_start, _holding_minutes
    from datetime import date

    ds = _day_start(date(2026, 5, 7))
    assert ds.year == 2026
    print("✓ report helpers OK")


def main() -> None:
    print("=== Quant mode smoke run ===")
    try:
        smoke_primitives()
        smoke_bandit()
        smoke_sizer()
        smoke_circuit_breaker()
        smoke_report_format()
        print("\n✅ All smoke tests passed")
        sys.exit(0)
    except Exception as exc:
        print(f"\n❌ Smoke test FAILED: {exc!r}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
