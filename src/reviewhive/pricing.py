"""What a review costs.

Rates are facts about Anthropic's published price list, not deployment
configuration, so they live here rather than in `Settings`. Making them settings
would mean three env vars per model that nobody ever sets, each one a typo away
from silently corrupting a cost history that cannot be recomputed after the fact.

This module imports `models` and never the reverse, which keeps the domain types
free of pricing and leaves exactly one place that knows what a token is worth.

Prices are USD per million tokens, from
https://platform.claude.com/docs/en/about-claude/models/overview
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from reviewhive.models import AgentCall, ReviewResult

logger = logging.getLogger(__name__)

_PER_MTOK = Decimal(1_000_000)


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_read_per_mtok: Decimal


def _price(inp: str, out: str, cache_read: str) -> ModelPrice:
    # Built from strings so the rates are exact: Decimal(0.1) is not one tenth.
    return ModelPrice(Decimal(inp), Decimal(out), Decimal(cache_read))


# Cache reads are ~0.1x the input rate on every current model.
PRICES: dict[str, ModelPrice] = {
    "claude-haiku-4-5": _price("1.00", "5.00", "0.10"),
    "claude-sonnet-4-6": _price("3.00", "15.00", "0.30"),
    "claude-sonnet-5": _price("3.00", "15.00", "0.30"),
    "claude-opus-4-6": _price("5.00", "25.00", "0.50"),
    "claude-opus-4-7": _price("5.00", "25.00", "0.50"),
    "claude-opus-4-8": _price("5.00", "25.00", "0.50"),
    "claude-opus-5": _price("5.00", "25.00", "0.50"),
}


def price_for(model: str) -> ModelPrice | None:
    return PRICES.get(model)


def cost_of(call: AgentCall) -> Decimal | None:
    """What one agent call cost, or `None` if the model has no published rate here.

    `None` rather than zero: an unpriced model must stay distinguishable from a
    free one. A silent zero would make a missing price look like a bargain, and
    `SUM` would quietly under-report while `WHERE cost_usd IS NULL` still finds
    the gap.

    Cache reads are billed separately because the API reports
    `cache_read_input_tokens` alongside — not inside — `input_tokens`. The term is
    zero today since nothing sets `cache_control`, and stops being wrong the day
    something does.
    """
    price = price_for(call.model)
    if price is None:
        logger.warning("no price for model %r; cost recorded as unknown", call.model)
        return None

    return (
        call.input_tokens * price.input_per_mtok
        + call.output_tokens * price.output_per_mtok
        + call.cache_read_tokens * price.cache_read_per_mtok
    ) / _PER_MTOK


def total_cost(result: ReviewResult) -> Decimal:
    """The run's cost, counting only calls whose model is priced.

    Deliberately not a property on `ReviewResult`: computing it there would pull
    the price table into the domain model, and a stored field would go stale the
    moment anything appended to `calls`. Callers that persist this should record
    the figure at write time, so it stays a snapshot of the prices in force when
    the run happened rather than being silently rewritten by a later rate change.
    """
    return sum((c for call in result.calls if (c := cost_of(call)) is not None), Decimal(0))
