"""The graph's shared state.

`findings` and `calls` carry `operator.add` reducers. That is what makes the
fan-out work: each agent node returns only the items it produced and LangGraph
concatenates them, so three nodes can write the same key concurrently without
clobbering each other. Everything else is last-write-wins, and only one node ever
writes each of those keys.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from reviewhive.diff.budget import BudgetedDiff
from reviewhive.models import AgentCall, MergedFinding, ReviewResult


class ReviewState(TypedDict, total=False):
    # Input
    diff_text: str

    # Written by prepare_diff
    budget: BudgetedDiff

    # Written concurrently by the agent nodes
    findings: Annotated[list[MergedFinding], operator.add]
    calls: Annotated[list[AgentCall], operator.add]

    # Written by finalize
    result: ReviewResult
