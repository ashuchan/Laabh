"""Tests for cross-agent Pydantic validators."""
import pytest
from pydantic import ValidationError

from src.agents.validators import (
    Allocation,
    CEOJudgeOutputValidated,
    EquityExpertOutputValidated,
    VALIDATOR_REGISTRY,
)


VALID_JUDGE_OUTPUT = {
    "decision_summary": "Deploying into BANKNIFTY long call spread given rate-cut catalyst.",
    "disagreement_loci": [],
    "allocation": [
        {
            "asset_class": "fno",
            "underlying_or_symbol": "BANKNIFTY",
            "capital_pct": 25.0,
            "decision": "BUY_CALL_SPREAD",
            "conviction": 0.78,
        },
        {
            "asset_class": "cash",
            "underlying_or_symbol": "CASH",
            "capital_pct": 75.0,
            "decision": "HOLD",
        },
    ],
    "kill_switches": [
        {
            "trigger": "BANKNIFTY falls below 48000",
            "action": "exit_all",
            "monitoring_metric": "BANKNIFTY spot < 48000",
        }
    ],
    "ceo_note": "Today's setup is clear. RBI rate-cut chatter is driving IV low. "
                "We deploy 25% into a debit spread with defined risk. "
                "The bear case is the RBI stays on hold, which we hedge via small size. "
                "Kill-switch is a 2% adverse move in BANKNIFTY. "
                "This is a workable but not extraordinary setup.",
    "calibration_self_check": {
        "bullish_argument_grade": "B",
        "bearish_argument_grade": "B",
        "confidence_in_allocation": 0.72,
        "regret_scenario": "Worse to miss a 12% winner than to risk a 3% loser.",
    },
    "expected_book_pnl_pct": 8.5,
    "stretch_pnl_pct": 14.0,
    "max_drawdown_tolerated_pct": 3.0,
}


class TestCEOJudgeOutputValidated:
    def test_valid_output_passes(self):
        result = CEOJudgeOutputValidated(**VALID_JUDGE_OUTPUT)
        assert result.expected_book_pnl_pct == 8.5

    def test_capital_over_100_fails(self):
        data = {**VALID_JUDGE_OUTPUT}
        data["allocation"] = [
            {"asset_class": "fno", "underlying_or_symbol": "NIFTY",
             "capital_pct": 60.0, "decision": "BUY"},
            {"asset_class": "equity", "underlying_or_symbol": "TCS",
             "capital_pct": 60.0, "decision": "BUY"},
        ]
        with pytest.raises(ValidationError, match="sums to"):
            CEOJudgeOutputValidated(**data)

    def test_single_position_over_40pct_fails(self):
        data = {**VALID_JUDGE_OUTPUT}
        data["allocation"] = [
            {"asset_class": "fno", "underlying_or_symbol": "BANKNIFTY",
             "capital_pct": 45.0, "decision": "BUY"},
            {"asset_class": "cash", "underlying_or_symbol": "CASH",
             "capital_pct": 55.0, "decision": "HOLD"},
        ]
        with pytest.raises(ValidationError, match="40%"):
            CEOJudgeOutputValidated(**data)

    def test_negative_pnl_fails(self):
        data = {**VALID_JUDGE_OUTPUT, "expected_book_pnl_pct": -2.0}
        with pytest.raises(ValidationError, match="positive"):
            CEOJudgeOutputValidated(**data)

    def test_drawdown_over_10pct_fails(self):
        data = {**VALID_JUDGE_OUTPUT, "max_drawdown_tolerated_pct": 15.0}
        with pytest.raises(ValidationError, match="10%"):
            CEOJudgeOutputValidated(**data)

    def test_allocation_can_be_under_100(self):
        data = {**VALID_JUDGE_OUTPUT}
        data["allocation"] = [
            {"asset_class": "cash", "underlying_or_symbol": "CASH",
             "capital_pct": 100.0, "decision": "HOLD"},
        ]
        result = CEOJudgeOutputValidated(**data)
        assert result.allocation[0].capital_pct == 100.0

    def test_kill_switches_required_shape(self):
        data = {**VALID_JUDGE_OUTPUT}
        data["kill_switches"] = [
            {"trigger": "X", "action": "exit_all", "monitoring_metric": "Y"}
        ]
        result = CEOJudgeOutputValidated(**data)
        assert result.kill_switches[0].action == "exit_all"


class TestEquityExpertOutputValidated:
    def test_valid_buy(self):
        result = EquityExpertOutputValidated(
            symbol="TATAMOTORS",
            decision="BUY",
            conviction=0.75,
            refused=False,
            entry_zone={"low": 900.0, "high": 910.0},
            target=950.0,
            stop=880.0,
        )
        assert result.decision == "BUY"

    def test_buy_with_target_below_entry_fails(self):
        with pytest.raises(ValidationError, match="target"):
            EquityExpertOutputValidated(
                symbol="TATAMOTORS",
                decision="BUY",
                conviction=0.75,
                refused=False,
                entry_zone={"low": 900.0, "high": 910.0},
                target=800.0,   # target below entry
                stop=880.0,
            )

    def test_buy_with_stop_above_entry_fails(self):
        with pytest.raises(ValidationError, match="stop"):
            EquityExpertOutputValidated(
                symbol="TATAMOTORS",
                decision="BUY",
                conviction=0.75,
                refused=False,
                entry_zone={"low": 900.0, "high": 910.0},
                target=950.0,
                stop=920.0,   # stop above entry
            )

    def test_refused_skips_price_validation(self):
        result = EquityExpertOutputValidated(
            symbol="TATAMOTORS",
            decision="REFUSE",
            conviction=0.3,
            refused=True,
            entry_zone={"low": 900.0, "high": 910.0},
            target=800.0,   # would normally fail
            stop=920.0,
        )
        assert result.refused is True


class TestValidatorRegistry:
    def test_all_validators_registered(self):
        assert "CEOJudgeOutputValidated" in VALIDATOR_REGISTRY
        assert "EquityExpertOutputValidated" in VALIDATOR_REGISTRY

    def test_registry_values_are_classes(self):
        for name, cls in VALIDATOR_REGISTRY.items():
            assert callable(cls), f"{name} is not callable"
