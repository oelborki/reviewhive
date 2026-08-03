"""Turning a mention into an action.

The classifier decides what was asked; this decides what to do about it, and the
routing is ordinary code so a misread shows up as a logged rationale rather than
as behaviour hidden inside a reply.

Four paths, two of which reply in the thread and two of which post a review:

    full_review     -> the graph, unfocused
    focused_review  -> the graph, narrowed
    answer_question -> one call, reply in the thread
    reconsider      -> one call, reply in the thread

Nothing here raises. A mention is a courtesy; failing to answer one must not take
down the handler or leave a review half-recorded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from uuid import UUID

from reviewhive.github.client import GitHubError
from reviewhive.jobs import JobDeps, PullRequestRef, review_pull_request
from reviewhive.mentions.intent import CommentIntent, PriorFinding, classify
from reviewhive.mentions.respond import answer_question, reconsider
from reviewhive.models import AgentCall, ReviewResult

logger = logging.getLogger(__name__)

# Accounts allowed to spend money by mentioning the bot. Anyone else is ignored
# silently: telling a stranger why their comment was refused is a worse answer
# than not reacting to it.
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


def as_prior(findings) -> list[PriorFinding]:
    """`StoredFinding` -> what the prompts are shown.

    The body is deliberately dropped here. It goes to `reconsider` explicitly for
    the one finding under challenge; putting all fifteen bodies in front of a
    classifier would cost tokens to make a four-way decision worse.
    """
    return [
        PriorFinding(
            ordinal=f.ordinal,
            file=f.file,
            line=f.line,
            severity=f.severity,
            title=f.title,
        )
        for f in findings
    ]


@dataclass
class MentionOutcome:
    """What a mention produced, for the caller to record."""

    intent: CommentIntent | None = None
    reply: str | None = None
    calls: list[AgentCall] = field(default_factory=list)
    review_dispatched: bool = False


async def respond_to_mention(
    deps: JobDeps,
    ref: PullRequestRef,
    review_id: UUID,
    *,
    comment: str,
    in_reply_to: int | None = None,
    comment_path: str | None = None,
    comment_line: int | None = None,
) -> None:
    """Read a mention, do what it asked, and say so. Never raises.

    The background half of the mention path, mirroring `review_pull_request`: it
    owns the whole lifecycle of one mention including recording what it cost, so
    the handler can answer 202 and stop thinking about it.
    """
    try:
        stored = await deps.store.latest_findings(
            repo_full_name=ref.repo_full_name, pr_number=ref.pr_number
        )
        findings = as_prior(stored)
        bodies = {f.ordinal: f.body for f in stored}

        # A reply inside a thread already names the line it is about, so the
        # finding is resolved from the payload rather than guessed. This is the
        # one place the model's answer is overridden by a fact.
        thread_target = _thread_target(stored, comment_path, comment_line) if in_reply_to else None

        outcome = await handle_mention(
            deps,
            ref,
            comment=comment,
            findings=findings,
            bodies=bodies,
            thread_target=thread_target,
            review_id=review_id,
        )

        if outcome.reply:
            await _post_reply(deps, ref, outcome.reply, in_reply_to)

        if not outcome.review_dispatched:
            # The review path already finished this row, with the classifier's
            # call folded in. Finishing it twice would duplicate the agent rows
            # and trip the one-call-per-agent constraint.
            await deps.store.finish_review(
                review_id,
                ReviewResult(findings=[], calls=outcome.calls),
                elapsed_ms=0,
            )
    except Exception as exc:
        logger.exception("unhandled error answering a mention on %s", ref.repo_full_name)
        try:
            await deps.store.fail_review(review_id, f"{type(exc).__name__}: {exc}")
        except Exception:
            logger.exception("could not record the mention failure")


def _thread_target(
    stored, path: str | None, line: int | None
) -> int | None:
    """Which finding an inline thread belongs to, from the comment's own anchor."""
    if not path:
        return None
    for finding in stored:
        if finding.file == path and (line is None or finding.line == line):
            return finding.ordinal
    return None


async def _post_reply(
    deps: JobDeps, ref: PullRequestRef, reply: str, in_reply_to: int | None
) -> None:
    """Answer where the question was asked."""
    try:
        if in_reply_to is not None:
            await deps.github.reply_to_review_comment(
                ref.repo_full_name, ref.pr_number, in_reply_to, reply
            )
        else:
            await deps.github.create_issue_comment(ref.repo_full_name, ref.pr_number, reply)
    except GitHubError:
        # The answer cost money and is lost, which is worth a log and not worth
        # failing the run over -- the calls are still recorded either way.
        logger.exception("could not post a reply")


async def handle_mention(
    deps: JobDeps,
    ref: PullRequestRef,
    *,
    comment: str,
    findings: list[PriorFinding],
    bodies: dict[int, str],
    thread_target: int | None = None,
    review_id: UUID | None = None,
) -> MentionOutcome:
    """Read a mention and carry out what it asked for.

    `findings` and `bodies` come from the last review of this pull request. Without
    them a question has nothing to be about and a rebuttal has nothing to argue
    with, so a mention on a pull request that was never reviewed can only sensibly
    become a review.
    """
    outcome = MentionOutcome()

    intent, call = await classify(
        deps.client,
        deps.settings,
        comment=comment,
        findings=findings,
        thread_target=thread_target,
    )
    outcome.intent = intent
    if call is not None:
        outcome.calls.append(call)

    if intent.action in ("full_review", "focused_review"):
        if review_id is None:
            logger.warning("no review row for a re-review; skipping")
            return outcome
        focus = intent.focus if intent.action == "focused_review" else None
        await review_pull_request(deps, ref, review_id, focus=focus, extra_calls=outcome.calls)
        outcome.review_dispatched = True
        return outcome

    if not findings:
        # Nothing was ever reported here, so there is nothing to explain or defend.
        # Saying so is better than answering about a review that does not exist.
        outcome.reply = (
            "I haven't reviewed this pull request yet, so there's nothing for me to "
            "explain. Mention me on its own and I'll review it."
        )
        return outcome

    # Fetched only on the paths that need it. The review paths above never reach
    # here, and `review_pull_request` fetches its own — doing it earlier would
    # spend a round trip that four out of five mentions throw away.
    try:
        diff_text = await deps.github.fetch_pull_request_diff(
            ref.repo_full_name, ref.pr_number
        )
    except GitHubError as exc:
        logger.warning("could not fetch the diff to answer a mention: %s", exc)
        return outcome

    target = _target(intent, findings)

    if intent.action == "reconsider" and target is not None:
        verdict, call = await reconsider(
            deps.client,
            deps.settings,
            rebuttal=comment,
            finding=target,
            body=bodies.get(target.ordinal, ""),
            diff_text=diff_text,
        )
        outcome.calls.append(call)
        outcome.reply = verdict.reply if verdict else None
        return outcome

    # A `reconsider` that named no real finding lands here too. There is nothing
    # to re-judge, but the reviewer still said something, and answering it is
    # better than silence.
    reply, call = await answer_question(
        deps.client,
        deps.settings,
        question=intent.question or comment,
        diff_text=diff_text,
        findings=findings,
        target=target,
    )
    outcome.calls.append(call)
    outcome.reply = reply
    return outcome


def _target(intent: CommentIntent, findings: list[PriorFinding]) -> PriorFinding | None:
    """The finding an intent points at, if it points at a real one.

    An ordinal the model invented is dropped rather than trusted. Answering about
    the wrong finding is worse than answering about none.
    """
    if intent.target_ordinal is None:
        return None
    for finding in findings:
        if finding.ordinal == intent.target_ordinal:
            return finding
    logger.warning("intent named finding %s, which does not exist", intent.target_ordinal)
    return None
