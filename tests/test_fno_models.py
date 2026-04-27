"""F&O ORM model instantiation and import tests."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from src.models.fno_ban import FNOBanList
from src.models.fno_candidate import FNOCandidate
from src.models.fno_chain import OptionsChain
from src.models.fno_cooldown import FNOCooldown
from src.models.fno_iv import IVHistory
from src.models.fno_ranker_config import RankerConfig
from src.models.fno_signal import FNOSignal, FNOSignalEvent
from src.models.fno_vix import VIXTick
from src.models.llm_audit_log import LLMAuditLog


def _uid() -> uuid.UUID:
    return uuid.uuid4()


def test_fno_ban_list_instantiates() -> None:
    row = FNOBanList(instrument_id=_uid(), ban_date=date.today(), source="NSE")
    assert row.ban_date == date.today()


def test_fno_candidate_instantiates() -> None:
    row = FNOCandidate(
        instrument_id=_uid(),
        run_date=date.today(),
        phase=1,
        passed_liquidity=True,
        atm_oi=100000,
        atm_spread_pct=0.003,
        avg_volume_5d=25000,
    )
    assert row.phase == 1
    assert row.passed_liquidity is True


def test_options_chain_instantiates() -> None:
    iid = _uid()
    row = OptionsChain(
        instrument_id=iid,
        snapshot_at=datetime.now(tz=timezone.utc),
        expiry_date=date.today(),
        strike_price=1000.0,
        option_type="CE",
        ltp=25.50,
        bid_price=25.00,
        ask_price=26.00,
        oi=50000,
    )
    assert row.option_type == "CE"
    assert row.strike_price == 1000.0


def test_vix_tick_instantiates() -> None:
    row = VIXTick(
        timestamp=datetime.now(tz=timezone.utc),
        vix_value=14.5,
        regime="neutral",
    )
    assert row.regime == "neutral"


def test_iv_history_instantiates() -> None:
    row = IVHistory(
        instrument_id=_uid(),
        date=date.today(),
        atm_iv=22.5,
        iv_rank_52w=45.0,
        iv_percentile_52w=50.0,
    )
    assert row.atm_iv == 22.5


def test_fno_signal_instantiates() -> None:
    row = FNOSignal(
        underlying_id=_uid(),
        strategy_type="long_call",
        expiry_date=date.today(),
        legs=[{"strike": 1000, "option_type": "CE", "action": "BUY", "qty_lots": 1}],
        entry_premium_net=25.0,
        status="proposed",
    )
    assert row.strategy_type == "long_call"
    assert row.status == "proposed"


def test_fno_signal_event_instantiates() -> None:
    row = FNOSignalEvent(
        signal_id=_uid(),
        from_status="proposed",
        to_status="paper_filled",
        reason="fill simulated",
    )
    assert row.to_status == "paper_filled"


def test_ranker_config_instantiates() -> None:
    row = RankerConfig(
        version="v1",
        weights={"directional": 0.30, "convergence": 0.20},
    )
    assert row.version == "v1"


def test_fno_cooldown_instantiates() -> None:
    row = FNOCooldown(
        underlying_id=_uid(),
        cooldown_until=datetime.now(tz=timezone.utc),
        reason="stop_hit",
    )
    assert row.reason == "stop_hit"


def test_llm_audit_log_instantiates() -> None:
    row = LLMAuditLog(
        caller="phase1.extractor",
        caller_ref_id=_uid(),
        model="claude-sonnet-4-20250514",
        temperature=0.0,
        prompt="test prompt",
        response='{"signals": []}',
        tokens_in=100,
        tokens_out=50,
        latency_ms=320,
    )
    assert row.caller == "phase1.extractor"
    assert row.temperature == 0.0
