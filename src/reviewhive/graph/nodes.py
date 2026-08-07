"""Graph node implementations.

Every node is a plain async function taking `(state, deps)`. Dependencies are bound
at graph-build time with `functools.partial` rather than pulled from a runtime
config or a module-level singleton — which is what lets a test build the whole
graph around a stub client with no patching.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from anthropic import AsyncAnthropic

from reviewhive.agents.base import AgentSpec, make_token_counter, run_agent
from reviewhive.anchors import anchor_findings
from reviewhive.config import Settings
from reviewhive.diff.budget import build_budget
from reviewhive.diff.parser import parse_diff
from reviewhive.graph.critic import review_findings
from reviewhive.graph.dedupe import collapse, rank_and_cut
from reviewhive.graph.llm_merge import merge_findings
from reviewhive.graph.state import ReviewState
from reviewhive.models import MergedFinding, ReviewResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Deps:
    client: AsyncAnthropic
    settings: Settings


async def prepare_diff(state: ReviewState, deps: Deps) -> ReviewState:
    """Parse the raw diff, drop what is not worth reviewing, and fit the budget."""
    parsed = parse_diff(state["diff_text"])
    budget = await build_budget(
        parsed,
        make_token_counter(deps.client, deps.settings.agent_model),
        max_prompt_tokens=deps.settings.max_prompt_tokens,
        max_file_diff_lines=deps.settings.max_file_diff_lines,
    )

    logger.info(
        "prepared diff: %d files, %d tokens, %d skipped, %d truncated",
        len(budget.files),
        budget.token_count,
        len(budget.skipped),
        len(budget.truncated),
    )
    return {"budget": budget}


async def run_agent_node(state: ReviewState, deps: Deps, spec: AgentSpec) -> ReviewState:
    """One agent's slice of the fan-out.

    Returns only what this agent produced; the reducers on `findings` and `calls`
    merge the three branches.
    """
    outcome = await run_agent(
        spec,
        deps.client,
        state["budget"].prompt_text,
        deps.settings,
        focus=state.get("focus"),
    )
    return {
        "findings": [MergedFinding.from_finding(f, spec.name) for f in outcome.findings],
        "calls": [outcome.call],
    }


async def finalize(state: ReviewState, deps: Deps) -> ReviewState:
    """Deduplicate, validate locations, rank, and cap."""
    settings = deps.settings
    budget = state.get("budget")
    raw = state.get("findings", [])

    merged = collapse(
        raw,
        line_tolerance=settings.dedupe_line_tolerance,
        title_similarity=settings.dedupe_title_similarity,
    )

    anchored = anchor_findings(merged, budget.files if budget else [])
    if anchored.dropped_files:
        logger.info(
            "dropped %d findings naming files outside the diff", len(anchored.dropped_files)
        )

    calls = state.get("calls", [])
    findings = anchored.findings
    retracted = 0

    # Before the merge pass, and that ordering is load-bearing. `dedupe._merge`
    # takes `max(severity)` across members and unions `sources`, so once a poached
    # finding has collapsed into the one it copied, the review holds a single
    # high-severity finding sourced by two lanes: the per-lane severities are gone
    # and the lane that poached is no longer distinguishable. After the merge there
    # is nothing left to reconcile.
    #
    # After anchoring for the same reasons the merge pass is: no tokens spent on
    # findings about to be dropped for naming a file outside the diff, and the lines
    # judged are the snapped ones rather than the ones the models reported.
    if settings.enable_critic:
        outcome = await review_findings(
            deps.client, settings, findings, budget.files if budget else []
        )
        findings, retracted = outcome.findings, outcome.retracted
        if outcome.call is not None:
            calls = [*calls, outcome.call]

    # Before ranking, so a merge frees a slot under `max_posted_findings` instead of
    # arriving too late to matter.
    if settings.enable_llm_merge:
        findings, merge_call = await merge_findings(deps.client, settings, findings)
        if merge_call is not None:
            calls = [*calls, merge_call]

    kept, suppressed = rank_and_cut(
        findings,
        min_confidence=settings.min_confidence,
        min_severity=settings.min_severity,
        max_posted=settings.max_posted_findings,
    )

    logger.info(
        "finalized: %d raw -> %d merged -> %d after critic and merge passes -> %d posted "
        "(%d retracted, %d suppressed, %d snapped)",
        len(raw),
        len(merged),
        len(findings),
        len(kept),
        retracted,
        suppressed,
        anchored.snapped,
    )

    return {
        "result": ReviewResult(
            findings=kept,
            suppressed_count=suppressed,
            retracted_count=retracted,
            skipped_files=budget.skipped if budget else [],
            truncated_files=budget.truncated if budget else [],
            calls=calls,
            # Echoed onto the result so the summary can disclose it and the store
            # can record it. A narrowed review that does not say it was narrowed
            # reads as a clean bill of health for the whole diff.
            focus=state.get("focus"),
        )
    }


