from __future__ import annotations

from decimal import Decimal

import pytest

from reviewhive.config import Settings
from reviewhive.models import AgentCall, ReviewResult
from reviewhive.pricing import PRICES, ModelPrice, cost_of, price_for, total_cost


def call(**kwargs) -> AgentCall:
    return AgentCall(
        agent=kwargs.pop("agent", "security"),
        model=kwargs.pop("model", "claude-haiku-4-5"),
        input_tokens=kwargs.pop("input_tokens", 0),
        output_tokens=kwargs.pop("output_tokens", 0),
        **kwargs,
    )


def test_the_default_agent_model_has_a_price() -> None:
    """The one linkage that must never break: the model the agents actually run on
    has to be in the table, or every review is silently unpriced."""
    assert price_for(Settings(anthropic_api_key="test").agent_model) is not None


def test_haiku_rates_are_pinned() -> None:
    """Pinned here rather than only via a rendered dollar string, so a rate edit
    fails a test named for pricing instead of one named for rendering."""
    assert PRICES["claude-haiku-4-5"] == ModelPrice(
        input_per_mtok=Decimal("1.00"),
        output_per_mtok=Decimal("5.00"),
        cache_read_per_mtok=Decimal("0.10"),
    )


class TestCostOfOneCall:
    def test_input_and_output_are_billed_at_their_own_rates(self) -> None:
        # 1000 in @ $1/MTok + 200 out @ $5/MTok
        assert cost_of(call(input_tokens=1000, output_tokens=200)) == Decimal("0.002")

    def test_cache_reads_are_billed(self) -> None:
        """The API reports `cache_read_input_tokens` alongside `input_tokens`, not
        inside it, so the term is additive. Zero today because nothing sets
        `cache_control` — this stops being wrong the day something does."""
        assert cost_of(call(cache_read_tokens=1_000_000)) == Decimal("0.10")

    def test_an_unpriced_model_costs_none_not_zero(self) -> None:
        """`None` keeps a missing price distinguishable from a free call. Zero would
        make an unpriced model look like a bargain and under-report a sum with no
        way to notice."""
        assert cost_of(call(model="claude-imaginary-9", input_tokens=1_000_000)) is None

    def test_a_failed_call_still_costs_its_tokens(self) -> None:
        """An agent that errored after the model replied was still billed."""
        assert cost_of(call(input_tokens=1000, error="boom")) == Decimal("0.001")

    def test_arithmetic_is_exact(self) -> None:
        """Decimal, not float: three calls of a tenth of a cent must sum to exactly
        three tenths, because these figures are summed and stored as money."""
        one = cost_of(call(input_tokens=1000))
        assert one * 3 == cost_of(call(input_tokens=3000))


class TestTotalCost:
    def test_sums_every_priced_call(self) -> None:
        result = ReviewResult(
            calls=[call(input_tokens=1000), call(agent="style", output_tokens=1000)]
        )
        assert total_cost(result) == Decimal("0.001") + Decimal("0.005")

    def test_skips_unpriced_calls_rather_than_failing(self) -> None:
        """Cost telemetry must never be able to take down a review."""
        result = ReviewResult(
            calls=[
                call(input_tokens=1000),
                call(agent="style", model="claude-imaginary-9", input_tokens=999_999),
            ]
        )
        assert total_cost(result) == Decimal("0.001")

    def test_a_review_with_no_calls_costs_nothing(self) -> None:
        """A lockfile-only diff routes straight to finalize and calls no agents."""
        assert total_cost(ReviewResult()) == Decimal(0)


@pytest.mark.parametrize("model", sorted(PRICES))
def test_every_listed_price_is_positive_and_ordered(model: str) -> None:
    price = PRICES[model]
    assert price.input_per_mtok > 0
    assert price.output_per_mtok > price.input_per_mtok, "output is dearer than input"
    assert price.cache_read_per_mtok < price.input_per_mtok, "a cache read is cheaper"
