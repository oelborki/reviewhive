"""The pass that checks the reviewers' claims against the code they are about.

`collapse` and `merge_findings` decide which findings are the *same*. Nothing before
this point asks whether any of them is *right*, and two measured defects follow from
that.

**A poached finding is rated higher than the lane that owns it.** On demo PR #5 the
architecture reviewer filed a whitelisted `ORDER BY` as high-severity SQL injection
while the security reviewer — which owns the category, and rates that same code
below high in twelve runs out of thirteen — filed it as medium. Ranking then showed
the reader the wrong one. Neither threshold can separate them: the poached findings
sit at 0.95 to 0.98 confidence because the vulnerabilities they copy are real, and
they rate *higher* on severity, so a floor on either deletes the correct finding and
keeps the wrong one. `sources` is what separates them, and it is already recorded.

**Every false positive measured so far shares one shape.** The model quotes a
compound condition correctly and then evaluates it backwards —
`not expected or not hmac.compare_digest(...)` filed as an authentication bypass
against a check that fails closed. That is a claim about code, so checking it needs
the code; a reader given only the finding reads a confident, coherent body and
agrees with it.

Four properties, in order of how badly their absence would hurt:

1. **A retraction is invisible.** A false positive left standing is noise a reader
   can dismiss. A true finding deleted here is gone, and nothing in the posted
   review says it ever existed. Everything below prefers leaving a finding alone,
   and `retracted_count` exists so that preference can be checked rather than
   assumed.
2. **Severity is never raised.** Refused in Python rather than asked for in the
   prompt. The defect this pass exists to fix is a finding rated above what its
   evidence supports; a pass that can inflate severity is one more thing that can
   commit it.
3. **Nothing raises.** A failed critic returns the findings untouched, exactly as
   `run_agent`, `intent.classify` and `merge_findings` do. Checking a review is an
   improvement on it, never a precondition for one.
4. **Selection is deterministic.** The model is only asked about findings this
   module chose, and only its answers about those are honoured. Which findings are
   asked about is pure Python the suite pins; only the answer needs a probe.

It runs *before* `merge_findings`, and that ordering is load-bearing. `dedupe._merge`
takes `max(severity)` across members and unions `sources`, so once the poached pair
has collapsed it is one high-severity finding sourced by two lanes — the per-lane
severities are gone and the lane that poached is no longer distinguishable. After
the merge there is nothing left to reconcile.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from anthropic import APIError, AsyncAnthropic
from pydantic import BaseModel, Field

from reviewhive.agents.base import load_prompt
from reviewhive.config import Settings
from reviewhive.diff.parser import DiffFile
from reviewhive.graph.dedupe import normalize_path
from reviewhive.models import SEVERITY_RANK, AgentCall, MergedFinding, Severity

logger = logging.getLogger(__name__)

PROMPT_FILE = "critic.md"


class CriticVerdict(BaseModel):
    """One verdict on one finding.

    Field order matters here for the same reason it does in `Finding`: generation is
    autoregressive, so `reason` is written last, after the verdict it explains rather
    than before it. A reason written first becomes an argument the verdict then has
    to agree with.
    """

    index: int = Field(description="Index of the finding, exactly as given.")
    verdict: Literal["keep", "amend", "drop"] = Field(
        description=(
            "keep: the finding stands as written. amend: the claim is real but the "
            "severity, title or body is wrong. drop: the claim is refuted by the "
            "lines shown."
        )
    )
    revised_severity: Severity | None = Field(
        default=None, description="Only on amend, and only ever lower than the current one."
    )
    revised_title: str | None = Field(
        default=None, description="Only on amend. Under 80 characters, the claim alone."
    )
    revised_body: str | None = Field(
        default=None, description="Only on amend. Two to four sentences."
    )
    reason: str = Field(description="Why, in one sentence, naming the line that settles it.")


class CriticVerdicts(BaseModel):
    verdicts: list[CriticVerdict]


@dataclass
class CriticOutcome:
    """What one critic pass produced, in the shape `AgentOutcome` already uses.

    `honoured` carries the verdicts that survived the refusals below, keyed by the
    index of the finding they were about. The graph ignores it; `probe_critic.py`
    needs it, and having the probe reconstruct verdicts by comparing findings before
    and after would be a second implementation of this module's rules — which is the
    probe-fidelity failure this project has already paid for three times.
    """

    findings: list[MergedFinding]
    retracted: int = 0
    honoured: dict[int, CriticVerdict] = field(default_factory=dict)
    call: AgentCall | None = None


def judgeable(
    findings: list[MergedFinding],
    files: list[DiffFile],
    *,
    radius: int,
    max_findings: int,
) -> dict[int, str]:
    """Which findings can be judged, and the lines each is judged against.

    A finding with no anchor gets no window, and a verdict reached without evidence
    is the guess this pass must not make — so file-level findings pass through
    untouched rather than being judged on their wording. The same goes for a finding
    whose anchor reaches no hunk, though `anchor_findings` has already dropped most
    of those.

    Capped for cost, and the cap keeps the earliest findings: they arrive in the
    order the agents produced them, and truncating the tail leaves a prefix that is
    still a fair sample rather than a filtered one.
    """
    by_path = {normalize_path(f.path): f for f in files}
    windows: dict[int, str] = {}

    for index, finding in enumerate(findings):
        if finding.line is None:
            continue
        diff_file = by_path.get(normalize_path(finding.file))
        if diff_file is None:
            continue
        window = diff_file.window(finding.line, radius=radius)
        if not window:
            continue

        windows[index] = window
        if len(windows) >= max_findings:
            logger.info("critic pass capped at %d findings", max_findings)
            break

    return windows


async def review_findings(
    client: AsyncAnthropic,
    settings: Settings,
    findings: list[MergedFinding],
    files: list[DiffFile],
) -> CriticOutcome:
    """Check each finding against its own lines. Never raises.

    An outcome carrying no call means there was nothing judgeable: an empty question
    costs nothing and is not worth a row. Every other failure returns the findings
    exactly as they arrived, with the call recording why.
    """
    windows = judgeable(
        findings,
        files,
        radius=settings.critic_context_radius,
        max_findings=settings.critic_max_findings,
    )
    if not windows:
        return CriticOutcome(findings=findings)

    started = time.perf_counter()
    call = AgentCall(agent="critic", model=settings.agent_model, input_tokens=0, output_tokens=0)

    try:
        message = await client.messages.parse(
            model=settings.agent_model,
            max_tokens=settings.agent_max_tokens,
            temperature=settings.agent_temperature,
            system=load_prompt(PROMPT_FILE, shared=False),
            messages=[{"role": "user", "content": _user_message(findings, windows)}],
            output_format=CriticVerdicts,
        )
    except APIError as exc:
        logger.warning("critic pass failed: %s", exc)
        call.error = f"{type(exc).__name__}: {exc}"
        call.latency_ms = _elapsed_ms(started)
        return CriticOutcome(findings=findings, call=call)

    call.latency_ms = _elapsed_ms(started)
    call.input_tokens = message.usage.input_tokens
    call.output_tokens = message.usage.output_tokens
    call.cache_read_tokens = message.usage.cache_read_input_tokens or 0

    verdicts = message.parsed_output
    if verdicts is None or message.stop_reason == "refusal":
        call.error = f"unusable output (stop_reason={message.stop_reason})"
        logger.warning("critic pass returned nothing usable; findings left as they were")
        return CriticOutcome(findings=findings, call=call)

    if message.stop_reason == "max_tokens":
        # Truncated verdicts are still valid verdicts about the findings they cover.
        # The rest are simply left alone, which is the safe direction.
        call.error = "truncated at max_tokens"
        logger.warning("critic pass hit max_tokens; some findings went unjudged")

    kept, retracted, amended, honoured = _apply(findings, verdicts.verdicts, set(windows))
    logger.info(
        "critic pass: %d of %d findings judged, %d amended, %d retracted",
        len(windows),
        len(findings),
        amended,
        retracted,
    )
    return CriticOutcome(findings=kept, retracted=retracted, honoured=honoured, call=call)


def _apply(
    findings: list[MergedFinding],
    verdicts: list[CriticVerdict],
    asked: set[int],
) -> tuple[list[MergedFinding], int, int, dict[int, CriticVerdict]]:
    """Apply the verdicts that were asked for, refuse the rest.

    Three things are refused in code rather than in the prompt, because a prompt is
    a request and this is a guarantee:

    - **A verdict on a finding nobody asked about.** An invented index would
      otherwise edit or delete an arbitrary finding, and the one most likely to be
      invented is the one that was never shown.
    - **A second verdict on the same finding.** The first is honoured; a
      contradicting pair is a sign the output is confused, not an invitation to
      pick.
    - **A severity increase.** This pass exists because a finding was rated above
      what its evidence supports. Something that can inflate severity can commit
      the same defect it was built to correct.

    Order is preserved: a review that reshuffles itself after being checked is
    harder to compare against the one before it.
    """
    seen: dict[int, CriticVerdict] = {}
    for verdict in verdicts:
        if verdict.index not in asked:
            logger.warning("critic judged a finding it was not asked about: %d", verdict.index)
            continue
        if verdict.index in seen:
            logger.warning("critic returned two verdicts for finding %d; keeping the first",
                           verdict.index)
            continue
        seen[verdict.index] = verdict

    kept: list[MergedFinding] = []
    retracted = 0
    amended = 0

    for index, finding in enumerate(findings):
        verdict = seen.get(index)
        if verdict is None or verdict.verdict == "keep":
            kept.append(finding)
            continue

        if verdict.verdict == "drop":
            retracted += 1
            logger.info(
                "critic retracted %s:%s (%s) — %s",
                finding.file, finding.line, finding.title, verdict.reason,
            )
            continue

        updates = _amendments(finding, verdict)
        if not updates:
            # An amendment that changes nothing is a keep with extra words.
            kept.append(finding)
            continue

        amended += 1
        kept.append(finding.model_copy(update={**updates, "amended": True}))

    return kept, retracted, amended, seen


def _amendments(finding: MergedFinding, verdict: CriticVerdict) -> dict[str, object]:
    updates: dict[str, object] = {}

    severity = verdict.revised_severity
    if severity is not None and severity != finding.severity:
        if SEVERITY_RANK[severity] > SEVERITY_RANK[finding.severity]:
            logger.warning(
                "critic tried to raise %s:%s from %s to %s; refused",
                finding.file, finding.line, finding.severity, severity,
            )
        else:
            updates["severity"] = severity

    title = (verdict.revised_title or "").strip()
    if title and title != finding.title:
        updates["title"] = title

    body = (verdict.revised_body or "").strip()
    if body and body != finding.body:
        updates["body"] = body

    return updates


def _user_message(findings: list[MergedFinding], windows: dict[int, str]) -> str:
    """Each judgeable finding, with the lines it is a claim about.

    The window is the point of the whole call. `merge_findings` is told the opposite
    — it gets bodies and no code, because sameness is a question about two
    descriptions. This is a question about one description and the thing it
    describes, and without the second half the reader can only agree with whatever
    the body asserts.
    """
    blocks = [
        f"<finding index=\"{index}\">\n"
        f"{_render(findings[index])}\n"
        f"<lines>\n{windows[index].rstrip()}\n</lines>\n"
        f"</finding>"
        for index in sorted(windows)
    ]

    return (
        "Check each finding below against the lines it is about.\n\n" + "\n\n".join(blocks)
    )


def _render(finding: MergedFinding) -> str:
    return (
        f"location: {finding.file}:{finding.line}\n"
        f"reported by: {', '.join(finding.sources)}\n"
        f"severity: {finding.severity}\n"
        f"category: {finding.category}\n"
        f"title: {finding.title}\n"
        f"body: {finding.body}"
    )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
