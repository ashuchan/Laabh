"""Tests for intraday universe expansion — scanner, restless bandit, selectors."""
from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
import pytz

from src.quant.backtest.universe_top_gainers import TopGainersUniverseSelector
from src.quant.bandit.selector import ArmSelector

_IST = pytz.timezone("Asia/Kolkata")
# A fixed market-hours timestamp well before the 12:30 stop gate.
_MARKET_TIME_IST = _IST.localize(
    __import__("datetime").datetime(2026, 5, 12, 10, 30, 0)
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_candidate(symbol: str, sector: str | None, pct: float) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "symbol": symbol,
        "name": symbol,
        "sector": sector,
        "prev_day_return": pct / 100.0,
        "avg_volume_5d": 50_000,
        "prev_close": 500.0,
        "overnight_gap": None,
    }


# ---------------------------------------------------------------------------
# _build_sector_heat_bucket
# ---------------------------------------------------------------------------

class TestSectorHeatBucket:
    def test_hot_sector_adds_top_n(self):
        candidates = [
            _make_candidate("SUNPHARMA", "Pharma", 5.0),
            _make_candidate("CIPLA", "Pharma", 4.0),
            _make_candidate("DRREDDY", "Pharma", 3.0),
            _make_candidate("INFY", "IT", 0.5),   # IT not hot
        ]
        result = TopGainersUniverseSelector._build_sector_heat_bucket(
            candidates, threshold_pct=2.0, per_sector_count=2
        )
        symbols = [c["symbol"] for c in result]
        assert "SUNPHARMA" in symbols
        assert "CIPLA" in symbols
        # Only top 2 from Pharma
        assert len([c for c in result if c["sector"] == "Pharma"]) == 2
        # IT below threshold: not included
        assert "INFY" not in symbols

    def test_cold_sector_excluded(self):
        candidates = [
            _make_candidate("TCS", "IT", 0.3),
            _make_candidate("WIPRO", "IT", -0.2),
        ]
        result = TopGainersUniverseSelector._build_sector_heat_bucket(
            candidates, threshold_pct=1.5, per_sector_count=5
        )
        assert result == []

    def test_no_sector_field_skipped(self):
        candidates = [
            _make_candidate("ANON1", None, 8.0),
        ]
        result = TopGainersUniverseSelector._build_sector_heat_bucket(
            candidates, threshold_pct=1.5, per_sector_count=5
        )
        assert result == []

    def test_negative_sector_heat_triggers(self):
        # A sector in heavy sell-off is also "hot" in the absolute sense
        candidates = [
            _make_candidate("IRCTC", "Travel", -4.0),
            _make_candidate("INDIGO", "Travel", -3.5),
        ]
        result = TopGainersUniverseSelector._build_sector_heat_bucket(
            candidates, threshold_pct=2.0, per_sector_count=3
        )
        symbols = [c["symbol"] for c in result]
        assert "IRCTC" in symbols
        assert "INDIGO" in symbols

    def test_empty_candidates(self):
        assert TopGainersUniverseSelector._build_sector_heat_bucket(
            [], threshold_pct=1.5, per_sector_count=5
        ) == []


# ---------------------------------------------------------------------------
# ArmSelector dormant pool (Thompson)
# ---------------------------------------------------------------------------

class TestArmSelectorDormantPoolThompson:
    def _sel(self, arms: list[str]) -> ArmSelector:
        return ArmSelector(arms, algo="thompson", seed=42)

    def test_evict_saves_to_dormant(self):
        sel = self._sel(["INFY_mom", "TCS_mom"])
        sel.update("INFY_mom", 0.05)
        pre_mean = sel.posterior_mean("INFY_mom")
        pre_obs = sel.n_obs("INFY_mom")

        sel.evict_arm("INFY_mom")
        assert "INFY_mom" in sel.dormant_arm_ids

        # After eviction, the active set no longer knows the arm (cold default).
        assert sel.n_obs("INFY_mom") == 0

        # Re-admit and verify both mean and obs are fully restored from dormant.
        sel.admit_arm("INFY_mom")
        assert sel.posterior_mean("INFY_mom") == pytest.approx(pre_mean)
        assert sel.n_obs("INFY_mom") == pre_obs

    def test_readmit_from_dormant_restores_state(self):
        sel = self._sel(["INFY_mom", "TCS_mom"])
        sel.update("INFY_mom", 0.05)
        sel.update("INFY_mom", -0.02)
        pre_mean = sel.posterior_mean("INFY_mom")
        pre_obs = sel.n_obs("INFY_mom")

        sel.evict_arm("INFY_mom")
        warm = sel.admit_arm("INFY_mom")

        assert warm is True
        assert sel.posterior_mean("INFY_mom") == pytest.approx(pre_mean)
        assert sel.n_obs("INFY_mom") == pre_obs
        assert "INFY_mom" not in sel.dormant_arm_ids

    def test_cold_admit_for_unknown_arm(self):
        sel = self._sel(["INFY_mom"])
        warm = sel.admit_arm("BRAND_NEW_mom")
        assert warm is False
        assert sel.n_obs("BRAND_NEW_mom") == 0

    def test_replace_arm_evict_and_admit(self):
        sel = self._sel(["INFY_mom", "TCS_mom"])
        sel.update("INFY_mom", 0.1)
        pre_obs = sel.n_obs("INFY_mom")

        warm = sel.replace_arm("INFY_mom", "SUZLON_mom")
        assert warm is False  # SUZLON never seen before
        assert "SUZLON_mom" in [a for a in ["SUZLON_mom"] if sel.n_obs(a) == 0]

        # Re-replace: put INFY back from dormant
        warm2 = sel.replace_arm("SUZLON_mom", "INFY_mom")
        assert warm2 is True
        assert sel.n_obs("INFY_mom") == pre_obs

    def test_dormant_arms_property(self):
        sel = self._sel(["A_mom", "B_mom", "C_mom"])
        sel.evict_arm("A_mom")
        sel.evict_arm("B_mom")
        assert set(sel.dormant_arm_ids) == {"A_mom", "B_mom"}

    def test_evict_nonexistent_arm_noop(self):
        sel = self._sel(["A_mom"])
        sel.evict_arm("GHOST_mom")  # must not raise
        assert sel.dormant_arm_ids == []


# ---------------------------------------------------------------------------
# ArmSelector dormant pool (LinTS)
# ---------------------------------------------------------------------------

class TestArmSelectorDormantPoolLinTS:
    def _sel(self, arms: list[str]) -> ArmSelector:
        return ArmSelector(arms, algo="lints", seed=42)

    def _ctx(self) -> np.ndarray:
        return np.array([0.5, 0.3, 0.6, 0.5, 0.4])

    def test_warm_readmit_preserves_n_obs(self):
        sel = self._sel(["INFY_mom", "TCS_mom"])
        ctx = self._ctx()
        sel.update("INFY_mom", 0.05, context=ctx)
        sel.update("INFY_mom", -0.01, context=ctx)
        pre_obs = sel.n_obs("INFY_mom")

        sel.evict_arm("INFY_mom")
        warm = sel.admit_arm("INFY_mom")

        assert warm is True
        assert sel.n_obs("INFY_mom") == pre_obs


# ---------------------------------------------------------------------------
# LiveGainersScanner.compute_replacements — unit-level with mocked DB
# ---------------------------------------------------------------------------

class TestLiveGainersScannerComputeReplacements:
    """Tests the pairing logic independent of DB calls."""

    def _scanner(self):
        from src.quant.live_gainers_scanner import LiveGainersScanner, LiveMover, ReplacementPair
        return LiveGainersScanner(), LiveMover, ReplacementPair

    def _mover(self, symbol: str, pct: float, inst_id=None) -> Any:
        from src.quant.live_gainers_scanner import LiveMover
        return LiveMover(
            id=inst_id or uuid.uuid4(),
            symbol=symbol,
            name=symbol,
            prev_close=500.0,
            current_price=500.0 * (1 + pct / 100),
            pct_change=pct,
            avg_volume_5d=50_000,
        )

    def _at_market_time(self):
        """Context manager that freezes datetime.now inside the scanner at 10:30 IST."""
        import src.quant.live_gainers_scanner as _mod
        from unittest.mock import MagicMock
        from datetime import datetime as _dt

        mock_dt = MagicMock(wraps=_dt)
        mock_dt.now.return_value = _MARKET_TIME_IST
        mock_dt.combine = _dt.combine
        return patch.object(_mod, "datetime", mock_dt)

    @pytest.mark.asyncio
    async def test_no_replacements_below_hysteresis(self):
        scanner, _, _ = self._scanner()
        active = [{"id": uuid.uuid4(), "symbol": "INFY", "name": "Infosys"}]
        selector = ArmSelector(["INFY_mom"], algo="thompson", seed=0)
        # Give INFY enough pulls to be eviction-eligible
        for _ in range(4):
            selector.update("INFY_mom", 0.0)

        # INFY moves 2%, candidate moves 3% — only 1% above, hysteresis is 1.5%
        with self._at_market_time(), \
             patch.object(scanner, "_fetch_live_movers", new_callable=AsyncMock) as mock_movers, \
             patch.object(scanner, "_load_ban_set", new_callable=AsyncMock) as mock_ban:
            mock_ban.return_value = set()
            mock_movers.return_value = [
                self._mover("INFY", 2.0),
                self._mover("SUZLON", 3.0),   # only 1% above INFY, below hysteresis
            ]
            pairs = await scanner.compute_replacements(
                active, selector, set(),
                trading_date=date(2026, 5, 12),
                primitives_list=["mom"],
            )
        assert pairs == []

    @pytest.mark.asyncio
    async def test_replacement_fires_above_hysteresis(self):
        scanner, _, _ = self._scanner()
        active = [{"id": uuid.uuid4(), "symbol": "INFY", "name": "Infosys"}]
        selector = ArmSelector(["INFY_mom"], algo="thompson", seed=0)
        for _ in range(4):
            selector.update("INFY_mom", 0.0)

        with self._at_market_time(), \
             patch.object(scanner, "_fetch_live_movers", new_callable=AsyncMock) as mock_movers, \
             patch.object(scanner, "_load_ban_set", new_callable=AsyncMock) as mock_ban:
            mock_ban.return_value = set()
            mock_movers.return_value = [
                self._mover("SUZLON", 5.0),   # candidate
                self._mover("INFY", 1.0),     # active, low momentum
            ]
            pairs = await scanner.compute_replacements(
                active, selector, set(),
                trading_date=date(2026, 5, 12),
                primitives_list=["mom"],
            )
        assert len(pairs) == 1
        assert pairs[0].evict_symbol == "INFY"
        assert pairs[0].admit_instrument["symbol"] == "SUZLON"

    @pytest.mark.asyncio
    async def test_open_position_blocks_eviction(self):
        scanner, _, _ = self._scanner()
        active = [{"id": uuid.uuid4(), "symbol": "INFY", "name": "Infosys"}]
        selector = ArmSelector(["INFY_mom"], algo="thompson", seed=0)
        for _ in range(4):
            selector.update("INFY_mom", 0.0)

        with self._at_market_time(), \
             patch.object(scanner, "_fetch_live_movers", new_callable=AsyncMock) as mock_movers, \
             patch.object(scanner, "_load_ban_set", new_callable=AsyncMock) as mock_ban:
            mock_ban.return_value = set()
            mock_movers.return_value = [
                self._mover("SUZLON", 8.0),
                self._mover("INFY", 0.5),
            ]
            # INFY has an open position
            pairs = await scanner.compute_replacements(
                active, selector, {"INFY"},
                trading_date=date(2026, 5, 12),
                primitives_list=["mom"],
            )
        assert pairs == []

    @pytest.mark.asyncio
    async def test_min_pulls_gate(self):
        scanner, _, _ = self._scanner()
        active = [{"id": uuid.uuid4(), "symbol": "INFY", "name": "Infosys"}]
        selector = ArmSelector(["INFY_mom"], algo="thompson", seed=0)
        # Only 1 pull — below min_pulls_before_evict=3 × 1 primitive = 3
        selector.update("INFY_mom", 0.0)

        with self._at_market_time(), \
             patch.object(scanner, "_fetch_live_movers", new_callable=AsyncMock) as mock_movers, \
             patch.object(scanner, "_load_ban_set", new_callable=AsyncMock) as mock_ban:
            mock_ban.return_value = set()
            mock_movers.return_value = [
                self._mover("SUZLON", 9.0),
                self._mover("INFY", 0.0),
            ]
            pairs = await scanner.compute_replacements(
                active, selector, set(),
                trading_date=date(2026, 5, 12),
                primitives_list=["mom"],
            )
        assert pairs == []

    @pytest.mark.asyncio
    async def test_underscore_symbol_open_position_respected(self):
        """BAJAJ_AUTO contains an underscore — ensure it is correctly identified
        as having an open position and not evicted."""
        scanner, _, _ = self._scanner()
        active = [{"id": uuid.uuid4(), "symbol": "BAJAJ_AUTO", "name": "Bajaj Auto"}]
        selector = ArmSelector(["BAJAJ_AUTO_mom"], algo="thompson", seed=0)
        for _ in range(4):
            selector.update("BAJAJ_AUTO_mom", 0.0)

        with self._at_market_time(), \
             patch.object(scanner, "_fetch_live_movers", new_callable=AsyncMock) as mock_movers, \
             patch.object(scanner, "_load_ban_set", new_callable=AsyncMock) as mock_ban:
            mock_ban.return_value = set()
            mock_movers.return_value = [
                self._mover("SUZLON", 8.0),
                self._mover("BAJAJ_AUTO", 0.3),
            ]
            pairs = await scanner.compute_replacements(
                active, selector, {"BAJAJ_AUTO"},
                trading_date=date(2026, 5, 12),
                primitives_list=["mom"],
            )
        assert pairs == []


# ---------------------------------------------------------------------------
# HybridUniverseSelector
# ---------------------------------------------------------------------------

class TestHybridUniverseSelector:
    @pytest.mark.asyncio
    async def test_supplements_appended_to_base(self):
        from src.quant.universe import HybridUniverseSelector

        base_instruments = [
            {"id": uuid.uuid4(), "symbol": "INFY", "name": "Infosys"},
            {"id": uuid.uuid4(), "symbol": "TCS", "name": "TCS"},
        ]
        llm_supplement = [
            {"id": uuid.uuid4(), "symbol": "SUNPHARMA", "name": "Sun Pharma"},
            {"id": uuid.uuid4(), "symbol": "INFY", "name": "Infosys"},  # dup — must be skipped
        ]

        sel = HybridUniverseSelector.__new__(HybridUniverseSelector)
        sel._llm_enabled = True
        sel._llm_max_add = 3
        sel._base = MagicMock()
        sel._base.select = AsyncMock(return_value=list(base_instruments))

        with patch.object(HybridUniverseSelector, "_load_llm_proceeds", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = llm_supplement
            result = await sel.select(date(2026, 5, 12))

        symbols = [r["symbol"] for r in result]
        assert "SUNPHARMA" in symbols
        assert symbols.count("INFY") == 1  # no duplicate

    @pytest.mark.asyncio
    async def test_llm_disabled_returns_base_only(self):
        from src.quant.universe import HybridUniverseSelector

        base = [{"id": uuid.uuid4(), "symbol": "INFY", "name": "Infosys"}]
        sel = HybridUniverseSelector.__new__(HybridUniverseSelector)
        sel._llm_enabled = False
        sel._llm_max_add = 5
        sel._base = MagicMock()
        sel._base.select = AsyncMock(return_value=base)

        result = await sel.select(date(2026, 5, 12))
        assert result == base

    @pytest.mark.asyncio
    async def test_llm_max_add_respected(self):
        from src.quant.universe import HybridUniverseSelector

        base = [{"id": uuid.uuid4(), "symbol": "INFY", "name": "Infosys"}]
        supplements = [
            {"id": uuid.uuid4(), "symbol": f"SYM{i}", "name": f"Stock{i}"}
            for i in range(10)
        ]

        sel = HybridUniverseSelector.__new__(HybridUniverseSelector)
        sel._llm_enabled = True
        sel._llm_max_add = 3
        sel._base = MagicMock()
        sel._base.select = AsyncMock(return_value=base)

        with patch.object(HybridUniverseSelector, "_load_llm_proceeds", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = supplements
            result = await sel.select(date(2026, 5, 12))

        # base (1) + max_add (3) = 4
        assert len(result) == 4
