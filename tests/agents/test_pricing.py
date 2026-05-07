"""Tests for the model pricing and cost computation utilities."""
import pytest
from decimal import Decimal

from src.agents.runtime.pricing import (
    MODEL_PRICES,
    compute_cost,
    project_agent_cost,
)


class FakeUsage:
    def __init__(self, input_tokens=0, output_tokens=0,
                 cache_read_input_tokens=0, cache_creation_input_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read_input_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens


class TestModelPrices:
    def test_all_three_models_present(self):
        assert "claude-haiku-4-5-20251001" in MODEL_PRICES
        assert "claude-sonnet-4-6" in MODEL_PRICES
        assert "claude-opus-4-7" in MODEL_PRICES

    def test_prices_are_decimal(self):
        for model, prices in MODEL_PRICES.items():
            for key, val in prices.items():
                assert isinstance(val, Decimal), f"{model}.{key} is not Decimal"

    def test_opus_most_expensive(self):
        haiku = MODEL_PRICES["claude-haiku-4-5-20251001"]["input"]
        sonnet = MODEL_PRICES["claude-sonnet-4-6"]["input"]
        opus = MODEL_PRICES["claude-opus-4-7"]["input"]
        assert haiku < sonnet < opus

    def test_cache_read_is_cheapest_per_model(self):
        for model, prices in MODEL_PRICES.items():
            assert prices["cache_read_input"] < prices["input"], f"{model} cache_read not cheapest"


class TestComputeCost:
    def test_zero_usage_is_zero_cost(self):
        usage = FakeUsage()
        cost = compute_cost("claude-sonnet-4-6", usage)
        assert cost == Decimal("0")

    def test_basic_input_output_cost(self):
        usage = FakeUsage(input_tokens=1_000_000, output_tokens=1_000_000)
        cost = compute_cost("claude-sonnet-4-6", usage)
        expected = Decimal("3.00") + Decimal("15.00")
        assert cost == expected

    def test_cache_read_tokens_cheaper(self):
        full_usage = FakeUsage(input_tokens=1_000_000, output_tokens=0)
        cached_usage = FakeUsage(input_tokens=1_000_000, output_tokens=0,
                                 cache_read_input_tokens=500_000)
        full_cost = compute_cost("claude-sonnet-4-6", full_usage)
        cached_cost = compute_cost("claude-sonnet-4-6", cached_usage)
        assert cached_cost < full_cost

    def test_unknown_model_falls_back_to_haiku(self):
        usage = FakeUsage(input_tokens=1_000_000, output_tokens=0)
        cost_unknown = compute_cost("claude-unknown-model", usage)
        cost_haiku = compute_cost("claude-haiku-4-5-20251001", usage)
        assert cost_unknown == cost_haiku

    def test_cost_is_decimal_not_float(self):
        usage = FakeUsage(input_tokens=100, output_tokens=50)
        cost = compute_cost("claude-opus-4-7", usage)
        assert isinstance(cost, Decimal)

    def test_cache_creation_adds_cost(self):
        base = FakeUsage(input_tokens=1_000_000, output_tokens=0)
        with_cache = FakeUsage(input_tokens=1_000_000, output_tokens=0,
                               cache_creation_input_tokens=1_000_000)
        base_cost = compute_cost("claude-sonnet-4-6", base)
        cached_cost = compute_cost("claude-sonnet-4-6", with_cache)
        assert cached_cost > base_cost


class TestProjectAgentCost:
    def test_haiku_is_cheapest(self):
        haiku = project_agent_cost("claude-haiku-4-5-20251001", 10_000, 1_000)
        sonnet = project_agent_cost("claude-sonnet-4-6", 10_000, 1_000)
        opus = project_agent_cost("claude-opus-4-7", 10_000, 1_000)
        assert haiku < sonnet < opus

    def test_proportional_to_tokens(self):
        small = project_agent_cost("claude-sonnet-4-6", 1_000, 100)
        large = project_agent_cost("claude-sonnet-4-6", 10_000, 1_000)
        assert large == small * 10

    def test_returns_decimal(self):
        cost = project_agent_cost("claude-sonnet-4-6", 12_000, 1_500)
        assert isinstance(cost, Decimal)
