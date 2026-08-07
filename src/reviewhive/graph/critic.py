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

import asyncio
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

    **Field order is behaviour, not style. Do not reorder these.** Generation is
    autoregressive, so a field is written with only the fields above it in context —
    the same property that made `severity` unreliable in `Finding` until it was moved
    below the body it grades.

    `verdict` sat second here, so it was committed with nothing but an index in
    front of it, and `reason` was then written to justify a choice already made. The
    docstring at the time argued for that order on the grounds that a reason written
    first "becomes an argument the verdict has to agree with". That is backwards: a
    reading the verdict has to agree with is exactly what this pass is for. On demo
    PR #7 the model kept a high-severity claim whose refutation was the line beside
    it, and whose own body contradicted its title.

    `reason` now comes first and is a *reading* of the code, not a justification.
    Everything below it is conditioned on that reading.
    """

    index: int = Field(description="Index of the finding, exactly as given.")
    # The reading. Written before anything that acts on it.
    reason: str = Field(
        description=(
            "What the code actually does at the finding's location, in one sentence, "
            "naming the line that settles it. Written before the verdict, so it "
            "decides the verdict rather than defending it."
        )
    )
    code_prevents_it: bool = Field(
        description=(
            "True only if something in the code stops the problem the finding "
            "describes from happening -- a check, a whitelist, a guard clause, a "
            "constant-time compare. False if the problem is real, even when other "
            "code nearby has the same flaw, and false when you simply cannot tell."
        )
    )
    # The judgements, each conditioned on the reading above.
    verdict: Literal["keep", "amend", "drop"] = Field(
        description=(
            "keep: the finding stands as written. amend: the claim is real but the "
            "severity, title or body is wrong. drop: the code refutes the claim."
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


class CriticVerdicts(BaseModel):
    verdicts: list[CriticVerdict]


@dataclass
class CriticOutcome:
    """What one critic pass produced, in the shape `AgentOutcome` already uses.

    `honoured` carries the verdicts that survived the refusals below, and `survivors`
    says what each input finding actually became — itself, an amended copy, or
    `None` if it was withdrawn. Both are keyed by the finding's index on the way in.

    The graph ignores both; `probe_critic.py` needs them. Having the probe work out
    what happened by comparing the lists before and after cannot be done correctly:
    two findings can share a file and a line, and an amendment changes the fields
    you would match on. Its first attempt matched survivors by file path, which
    silently scored two cases against one finding the moment they shared a file.
    Reconstructing this outside the module is a second implementation of its rules,
    which is the probe-fidelity failure this project has already paid for three
    times.
    """

    findings: list[MergedFinding]
    retracted: int = 0
    honoured: dict[int, CriticVerdict] = field(default_factory=dict)
    survivors: dict[int, MergedFinding | None] = field(default_factory=dict)
    call: AgentCall | None = None


def judgeable(
    findings: list[MergedFinding],
    files: list[DiffFile],
    *,
    max_findings: int,
) -> dict[int, DiffFile]:
    """Which findings can be judged, and the file each is judged against.

    A finding with no anchor is not judged: a verdict reached without evidence is the
    guess this pass exists to avoid, so file-level findings pass through untouched
    rather than being graded on their wording. The same goes for a line that reaches
    no hunk, though `anchor_findings` has already snapped or dropped most of those.

    Capped for cost, and the cap keeps the earliest findings: they arrive in the
    order the agents produced them, so truncating the tail leaves a prefix that is
    still a fair sample rather than a filtered one.
    """
    by_path = {normalize_path(f.path): f for f in files}
    judged: dict[int, DiffFile] = {}

    for index, finding in enumerate(findings):
        if finding.line is None:
            continue
        diff_file = by_path.get(normalize_path(finding.file))
        if diff_file is None or finding.line not in diff_file.anchorable_lines:
            continue

        judged[index] = diff_file
        if len(judged) >= max_findings:
            logger.info("critic pass capped at %d findings", max_findings)
            break

    return judged


async def review_findings(
    client: AsyncAnthropic,
    settings: Settings,
    findings: list[MergedFinding],
    files: list[DiffFile],
) -> CriticOutcome:
    """Check each finding against the code it is about. Never raises.

    **One call per file, run concurrently.** Judging every finding in a single call
    was measured to dilute the reading: asked about `share.py:96` alone, the pass
    finds `verify()` thirty-eight lines away and withdraws the claim; asked about it
    inside a batch of thirteen findings across six files, it keeps the same claim
    every time. Nothing about the file changed, only how much else was in front of
    it. A file is also the natural unit — the whole point of the pass is reading one
    file closely — and the branches merge the same way the reviewer fan-out does.

    The calls are summed into **one** `AgentCall`. It is one logical pass and
    `UNIQUE(review_id, agent)` allows one row per agent per review, so recording
    them separately would need a schema change to say something the total already
    says.

    An outcome carrying no call means there was nothing judgeable: an empty question
    costs nothing and is not worth a row. Every other failure returns the findings
    exactly as they arrived, with the call recording why.
    """
    judged = judgeable(findings, files, max_findings=settings.critic_max_findings)
    if not judged:
        return CriticOutcome(findings=findings)

    started = time.perf_counter()
    grouped = _by_file(judged)
    results = await asyncio.gather(
        *(
            _judge_one_file(client, settings, findings, judged, indices)
            for indices in grouped.values()
        )
    )

    call = AgentCall(agent="critic", model=settings.agent_model, input_tokens=0, output_tokens=0)
    verdicts: list[CriticVerdict] = []
    errors: list[str] = []

    for outcome in results:
        verdicts.extend(outcome.verdicts)
        call.input_tokens += outcome.input_tokens
        call.output_tokens += outcome.output_tokens
        call.cache_read_tokens += outcome.cache_read_tokens
        if outcome.error:
            errors.append(outcome.error)

    call.latency_ms = _elapsed_ms(started)
    if errors:
        # One file failing leaves the others' verdicts standing, the same way one
        # dead reviewer still leaves a review.
        call.error = "; ".join(errors)

    if not verdicts:
        logger.warning("critic pass returned nothing usable; findings left as they were")
        return CriticOutcome(findings=findings, call=call)

    kept, retracted, amended, honoured, survivors = _apply(findings, verdicts, set(judged))
    logger.info(
        "critic pass: %d of %d findings judged, %d amended, %d retracted",
        len(judged),
        len(findings),
        amended,
        retracted,
    )
    return CriticOutcome(
        findings=kept,
        retracted=retracted,
        honoured=honoured,
        survivors=survivors,
        call=call,
    )


@dataclass
class _FileOutcome:
    """One file's call: what it decided and what it cost."""

    verdicts: list[CriticVerdict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    error: str | None = None


async def _judge_one_file(
    client: AsyncAnthropic,
    settings: Settings,
    findings: list[MergedFinding],
    judged: dict[int, DiffFile],
    indices: list[int],
) -> _FileOutcome:
    """Ask about one file's findings. Never raises; a failure costs that file only."""
    path = judged[indices[0]].path

    try:
        message = await client.messages.parse(
            model=settings.agent_model,
            max_tokens=settings.agent_max_tokens,
            temperature=settings.agent_temperature,
            system=load_prompt(PROMPT_FILE, shared=False),
            messages=[
                {
                    "role": "user",
                    "content": _user_message(
                        findings,
                        {index: judged[index] for index in indices},
                        max_file_lines=settings.critic_max_file_lines,
                        radius=settings.critic_context_radius,
                    ),
                }
            ],
            output_format=CriticVerdicts,
        )
    except APIError as exc:
        logger.warning("critic pass failed on %s: %s", path, exc)
        return _FileOutcome(error=f"{path}: {type(exc).__name__}: {exc}")

    outcome = _FileOutcome(
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
        cache_read_tokens=message.usage.cache_read_input_tokens or 0,
    )

    parsed = message.parsed_output
    if parsed is None or message.stop_reason == "refusal":
        outcome.error = f"{path}: unusable output (stop_reason={message.stop_reason})"
        return outcome

    if message.stop_reason == "max_tokens":
        # Truncated verdicts are still valid verdicts about the findings they cover.
        # The rest are simply left alone, which is the safe direction.
        outcome.error = f"{path}: truncated at max_tokens"
        logger.warning("critic pass hit max_tokens on %s; some findings went unjudged", path)

    outcome.verdicts = parsed.verdicts
    return outcome


def _apply(
    findings: list[MergedFinding],
    verdicts: list[CriticVerdict],
    asked: set[int],
) -> tuple[
    list[MergedFinding], int, int, dict[int, CriticVerdict], dict[int, MergedFinding | None]
]:
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
    survivors: dict[int, MergedFinding | None] = {}
    retracted = 0
    amended = 0

    for index, finding in enumerate(findings):
        verdict = seen.get(index)
        if verdict is None or verdict.verdict == "keep":
            kept.append(finding)
            survivors[index] = finding
            continue

        if verdict.verdict == "drop":
            retracted += 1
            survivors[index] = None
            logger.info(
                "critic retracted %s:%s (%s) — %s",
                finding.file, finding.line, finding.title, verdict.reason,
            )
            continue

        updates = _amendments(finding, verdict)
        if not updates:
            # An amendment that changes nothing is a keep with extra words.
            kept.append(finding)
            survivors[index] = finding
            continue

        amended += 1
        revised = finding.model_copy(update={**updates, "amended": True})
        kept.append(revised)
        survivors[index] = revised

    return kept, retracted, amended, seen, survivors


def _amendments(finding: MergedFinding, verdict: CriticVerdict) -> dict[str, object]:
    updates: dict[str, object] = {}

    severity = verdict.revised_severity
    if severity is not None and severity != finding.severity:
        if SEVERITY_RANK[severity] > SEVERITY_RANK[finding.severity]:
            logger.warning(
                "critic tried to raise %s:%s from %s to %s; refused",
                finding.file, finding.line, finding.severity, severity,
            )
        elif not verdict.code_prevents_it:
            # A severity comes down because the code stops the thing happening, not
            # because the reviewer sounded excited. Enforced here because asking did
            # not work: told the whole file, the pass began lowering a true
            # missing-authorization finding on the grounds that neighbouring
            # endpoints were unguarded too — "a pre-existing pattern rather than a
            # new vulnerability", in its own words. How widespread a flaw is has
            # nothing to do with whether it is real, and the model has to commit to
            # a guard existing before the downgrade is honoured.
            logger.warning(
                "critic lowered %s:%s to %s without naming a guard; refused",
                finding.file, finding.line, severity,
            )
        elif finding.severity != "high":
            # Only a high finding can have overstated itself, and overstatement is
            # the whole subject of this pass. A finding that already rated itself
            # modestly has nothing to correct, and lowering it further only moves it
            # further down a ranked list nobody reads to the end of.
            #
            # Refused here rather than asked for in the prompt because the prompt
            # asked and was ignored: given a whole file to read, the pass grew
            # confident enough to take security's *correct* medium on a whitelisted
            # ORDER BY down to low, which is the exact finding it exists to protect.
            logger.warning(
                "critic tried to lower %s:%s from %s to %s; refused, only high is re-ratable",
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


def _user_message(
    findings: list[MergedFinding],
    judged: dict[int, DiffFile],
    *,
    max_file_lines: int,
    radius: int,
) -> str:
    """The code, once per file, with that file's findings listed against it.

    Showing the code at all is the point of the call. `merge_findings` is told the
    opposite — bodies and no code — because sameness is a question about two
    descriptions. This is a question about one description and the thing it
    describes, and without the second half the reader can only agree with whatever
    the body asserts.

    **Whole files rather than a window per finding, and that is what fixes the
    defect this grouping was written for.** A window is bounded by a line count, and
    a claim is not: on demo PR #7 a finding anchored at line 96 was refuted by a
    function at line 58, so a ten-line window showed the pass everything except the
    thing that settled it. It kept the claim, correctly by its own rule, and the
    answer was wrong. Grouping also stops the same file being sent once per finding
    in overlapping slices, so on a defect-dense file this costs *fewer* tokens than
    the windows did.

    The window survives as the fallback for a file too large to send whole. That
    bound is on the file, not on the claim, so it degrades to the old behaviour
    rather than to something new.
    """
    blocks: list[str] = []

    for path, indices in _by_file(judged).items():
        diff_file = judged[indices[0]]
        reported = "\n\n".join(
            f'<finding index="{index}">\n{_render(findings[index])}\n</finding>'
            for index in indices
        )

        if _line_count(diff_file) <= max_file_lines:
            code = diff_file.numbered_text.rstrip()
        else:
            # Too large to send whole. Each finding carries its own lines instead,
            # which is what every finding used to get.
            code = "\n\n".join(
                diff_file.window(findings[index].line, radius=radius).rstrip()
                for index in indices
                if findings[index].line is not None
            )

        blocks.append(f'<file path="{path}">\n<code>\n{code}\n</code>\n\n{reported}\n</file>')

    return (
        "Check each finding below against the code it is about. The code for each "
        "file is given once, before the findings reported on it.\n\n" + "\n\n".join(blocks)
    )


def _by_file(judged: dict[int, DiffFile]) -> dict[str, list[int]]:
    """Finding indices grouped by the file they sit in, input order preserved."""
    grouped: dict[str, list[int]] = {}
    for index in sorted(judged):
        grouped.setdefault(judged[index].path, []).append(index)
    return grouped


def _line_count(diff_file: DiffFile) -> int:
    return sum(len(hunk.numbered_lines) for hunk in diff_file.hunks)


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
