"""The second deduplication pass.

`dedupe.collapse` merges findings whose titles overlap. It is deterministic, cheap
and pinned by tests, and it leaves a documented residue: two agents describing the
same defect in words that share almost no tokens. Measured on a real pull request,
architecture filed "POST /tasks/{owner}/{id}/done endpoint lacks authentication"
and security filed "Missing authorization check in done endpoint" — one line
apart, both at 0.95, and no title similarity between them at all.

This module asks a model about exactly those leftovers.

Three properties, in order of how badly their absence would hurt:

1. **A false merge destroys a finding.** `reports.py:40` and `reports.py:44` on the
   demo pull request carried *identical* titles and were two different
   vulnerabilities — line 40 injects `owner`, line 44 injects `r['title']`.
   Merging them would have silently dropped a real one. Everything here is built
   to make a wrong merge harder than a missed one.
2. **Nothing raises.** A merge failure returns the findings untouched, exactly as
   `run_agent` and `intent.classify` do. Deduplication is an improvement on a
   review, never a precondition for one.
3. **Candidate selection is deterministic.** The model is only ever asked about
   pairs this module chose, and choosing them is pure Python the suite can pin.
   The offline tests cover which pairs are asked about; only the answer needs a
   probe.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from anthropic import APIError, AsyncAnthropic
from pydantic import BaseModel, Field

from reviewhive.agents.base import load_prompt
from reviewhive.config import Settings
from reviewhive.graph.dedupe import _Cluster, _merge, normalize_path
from reviewhive.models import AgentCall, MergedFinding

logger = logging.getLogger(__name__)

PROMPT_FILE = "merge.md"


class MergeDecision(BaseModel):
    """One verdict on one pair."""

    left: int = Field(description="Index of the first finding, as given.")
    right: int = Field(description="Index of the second finding, as given.")
    same_defect: bool = Field(
        description="True only if fixing one necessarily fixes the other."
    )
    reason: str = Field(description="Why, in one sentence.")


class MergeDecisions(BaseModel):
    decisions: list[MergeDecision]


@dataclass(frozen=True)
class Candidate:
    """A pair worth asking about."""

    left: int
    right: int


def candidate_pairs(
    findings: list[MergedFinding], *, window: int, max_pairs: int
) -> list[Candidate]:
    """Which pairs survived `collapse` close enough together to be worth a question.

    Same file, and either both anchored within `window` lines or both file-level.
    A line and a file-level finding are never paired: with no position to compare,
    proximity says nothing, which is the same rule `collapse` follows.

    Sources must be disjoint. Two findings from the *same* agent are that agent
    reporting two things, and it had the whole diff in front of it when it decided
    they were two — the `reports.py:40`/`44` pair is exactly this case, and
    second-guessing it is how a real vulnerability gets dropped.

    Capped, and the cap is a cost guard rather than a correctness one: pairs grow
    quadratically with findings on one file, and a defect-dense diff would
    otherwise turn one call into a very large one.
    """
    pairs: list[Candidate] = []

    for left in range(len(findings)):
        for right in range(left + 1, len(findings)):
            first, second = findings[left], findings[right]

            if normalize_path(first.file) != normalize_path(second.file):
                continue
            if set(first.sources) & set(second.sources):
                continue

            if first.line is None or second.line is None:
                if first.line is not None or second.line is not None:
                    continue
            elif abs(first.line - second.line) > window:
                continue

            pairs.append(Candidate(left=left, right=right))
            if len(pairs) >= max_pairs:
                logger.info("candidate pairs capped at %d", max_pairs)
                return pairs

    return pairs


async def merge_findings(
    client: AsyncAnthropic,
    settings: Settings,
    findings: list[MergedFinding],
) -> tuple[list[MergedFinding], AgentCall | None]:
    """Collapse the cross-lane duplicates `collapse` could not see. Never raises.

    Returns the findings and the call it cost, or `(findings, None)` when there was
    nothing to ask about — an empty question costs nothing and is not worth a row.
    """
    pairs = candidate_pairs(
        findings,
        window=settings.merge_line_window,
        max_pairs=settings.merge_max_pairs,
    )
    if not pairs:
        return findings, None

    started = time.perf_counter()
    call = AgentCall(agent="merge", model=settings.agent_model, input_tokens=0, output_tokens=0)

    try:
        message = await client.messages.parse(
            model=settings.agent_model,
            max_tokens=settings.agent_max_tokens,
            system=load_prompt(PROMPT_FILE, shared=False),
            messages=[{"role": "user", "content": _user_message(findings, pairs)}],
            output_format=MergeDecisions,
        )
    except APIError as exc:
        logger.warning("merge pass failed: %s", exc)
        call.error = f"{type(exc).__name__}: {exc}"
        call.latency_ms = _elapsed_ms(started)
        return findings, call

    call.latency_ms = _elapsed_ms(started)
    call.input_tokens = message.usage.input_tokens
    call.output_tokens = message.usage.output_tokens
    call.cache_read_tokens = message.usage.cache_read_input_tokens or 0

    decisions = message.parsed_output
    if decisions is None or message.stop_reason == "refusal":
        call.error = f"unusable output (stop_reason={message.stop_reason})"
        logger.warning("merge pass returned nothing usable; findings left as they were")
        return findings, call

    if message.stop_reason == "max_tokens":
        # Truncated decisions are still valid decisions about the pairs they
        # cover. The rest simply go unmerged, which is the safe direction.
        call.error = "truncated at max_tokens"
        logger.warning("merge pass hit max_tokens; some pairs went unjudged")

    merged = _apply(findings, decisions.decisions, pairs)
    logger.info(
        "merge pass: %d pairs asked, %d merged, %d findings -> %d",
        len(pairs),
        len(findings) - len(merged),
        len(findings),
        len(merged),
    )
    return merged, call


def _apply(
    findings: list[MergedFinding],
    decisions: list[MergeDecision],
    pairs: list[Candidate],
) -> list[MergedFinding]:
    """Group by the pairs judged the same, then merge each group.

    Only pairs that were actually asked about are honoured. A decision naming
    anything else is dropped rather than trusted — an index the model invented
    would otherwise merge two arbitrary findings.

    Grouping is transitive on purpose: if A is the same as B and B the same as C,
    all three are one defect. `dedupe._merge` then does the merging itself, which
    keeps one definition of what a representative is — most severe, then most
    confident, sources unioned, and confidence *not* raised by agreement.
    """
    asked = {(pair.left, pair.right) for pair in pairs}
    parent = list(range(len(findings)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for decision in decisions:
        if not decision.same_defect:
            continue
        key = (min(decision.left, decision.right), max(decision.left, decision.right))
        if key not in asked:
            logger.warning("merge pass judged a pair it was not asked about: %s", key)
            continue
        parent[find(key[0])] = find(key[1])

    groups: dict[int, list[MergedFinding]] = {}
    for index, finding in enumerate(findings):
        groups.setdefault(find(index), []).append(finding)

    # Root order follows first appearance, so the output keeps the input's order.
    return [
        _merge(_Cluster(path=normalize_path(members[0].file), members=members))
        for _, members in sorted(groups.items())
    ]


def _user_message(findings: list[MergedFinding], pairs: list[Candidate]) -> str:
    """The pairs, with the bodies.

    Bodies are included deliberately, and this is not the same call as the mention
    classifier — which is told the opposite, because it makes a four-way routing
    decision where fifteen bodies are tokens spent to make it worse. Here the body
    is the evidence. `reports.py:40` and `reports.py:44` are indistinguishable by
    title; only the body says one injects `owner` and the other `r['title']`.
    """
    blocks = []
    for pair in pairs:
        blocks.append(
            f"<pair left=\"{pair.left}\" right=\"{pair.right}\">\n"
            f"{_render(pair.left, findings[pair.left])}\n\n"
            f"{_render(pair.right, findings[pair.right])}\n"
            f"</pair>"
        )

    return (
        "Decide, for each pair below, whether the two findings describe the same "
        "defect.\n\n" + "\n\n".join(blocks)
    )


def _render(index: int, finding: MergedFinding) -> str:
    where = f"{finding.file}:{finding.line}" if finding.line else f"{finding.file} (file-level)"
    return (
        f"[{index}] {where}\n"
        f"reported by: {', '.join(finding.sources)}\n"
        f"severity: {finding.severity}\n"
        f"title: {finding.title}\n"
        f"body: {finding.body}"
    )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
