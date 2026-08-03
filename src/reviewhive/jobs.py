"""Reviewing a pull request, end to end.

This is the webhook's counterpart to `scripts/review_local.py`: the same
sequence, with the diff fetched instead of read and the result posted instead of
printed. It imports neither FastAPI nor SQLAlchemy, which is what lets the whole
path be tested offline with a stub client, the real graph, and an in-memory
store.

Nothing here raises. A background task's exception goes to the server's logger
and nowhere a human is looking, so every failure is recorded against the review
instead.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from reviewhive.config import Settings
from reviewhive.github.client import DiffTooLarge, GitHubClient, GitHubError, GitHubUnprocessable
from reviewhive.github.positions import ReviewPayload, build_review, degrade
from reviewhive.persistence import ReviewStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PullRequestRef:
    """The pull request under review."""

    repo_full_name: str
    pr_number: int
    head_sha: str


@dataclass(frozen=True)
class JobDeps:
    """Everything a job needs, bound once at startup.

    The same convention as `build_review_graph`: dependencies arrive at
    construction, so there are no module-level singletons and nothing to patch.
    The client and store are long-lived on purpose — a background task outlives
    the request that dispatched it, so anything scoped to that request would be
    closed before the job used it.
    """

    settings: Settings
    graph: Any
    github: GitHubClient
    store: ReviewStore


async def review_pull_request(
    deps: JobDeps,
    ref: PullRequestRef,
    review_id: UUID,
    *,
    focus: str | None = None,
) -> None:
    """Fetch, review, record, post."""
    try:
        diff_text = await _fetch(deps, ref, review_id)
        if diff_text is None:
            return

        await deps.store.mark_running(review_id, diff_text=diff_text)

        started = time.perf_counter()
        try:
            final_state = await deps.graph.ainvoke({"diff_text": diff_text, "focus": focus})
        except Exception as exc:
            await deps.store.fail_review(review_id, f"{type(exc).__name__}: {exc}")
            logger.exception("review failed for %s#%s", ref.repo_full_name, ref.pr_number)
            return
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        result = final_state["result"]

        # Persisted before posting, and the choice is between two orphans. This
        # way a failed post leaves a succeeded row with its findings and cost
        # recorded, and the review can be re-posted by hand. The other way round,
        # a crash in between leaves a review sitting on someone's pull request
        # that the database has never heard of — the worse orphan, because the
        # pull request is the surface a human is actually reading.
        try:
            await deps.store.finish_review(review_id, result, elapsed_ms=elapsed_ms)
        except Exception:
            # The review cost real money and real minutes. Losing it in order to
            # report a database problem is the wrong way round.
            logger.exception("review completed but could not be saved")

        await _post(deps, ref, review_id, build_review(result, commit_id=ref.head_sha))
    except Exception:
        logger.exception("unhandled error reviewing %s#%s", ref.repo_full_name, ref.pr_number)


async def _fetch(deps: JobDeps, ref: PullRequestRef, review_id: UUID) -> str | None:
    """The diff, or None if there will not be one."""
    try:
        return await deps.github.fetch_pull_request_diff(ref.repo_full_name, ref.pr_number)
    except DiffTooLarge as exc:
        # Known, permanent and explainable, so it is worth telling the pull
        # request about — coverage is always disclosed, including on a surface
        # that did not exist yesterday. Deliberately not generalised to every
        # exception: a bot that comments "I errored" on strangers' pull requests
        # is worse than one that stays quiet.
        await _say(
            deps,
            ref,
            "## reviewHive\n\nGitHub declined to serve the diff for this pull "
            "request, which it does above roughly 20,000 changed lines. Nothing "
            "was reviewed.",
        )
        await deps.store.fail_review(review_id, f"DiffTooLarge: {exc}")
        return None
    except GitHubError as exc:
        await deps.store.fail_review(review_id, f"{type(exc).__name__}: {exc}")
        logger.error("could not fetch the diff: %s", exc)
        return None


async def _post(
    deps: JobDeps, ref: PullRequestRef, review_id: UUID, payload: ReviewPayload
) -> None:
    try:
        posted_id, count = await _create_review(deps, ref, payload)
    except GitHubError:
        # Left as `succeeded`: it ran and produced findings, they just did not
        # reach anyone. `posted_review_id IS NULL AND status = 'succeeded'` finds
        # exactly these.
        logger.exception("review saved but could not be posted")
        return

    try:
        await deps.store.record_posted_review(
            review_id, posted_review_id=posted_id, comment_count=count
        )
    except Exception:
        logger.exception("review posted but the posting could not be recorded")


async def _create_review(
    deps: JobDeps, ref: PullRequestRef, payload: ReviewPayload
) -> tuple[int, int]:
    """Post the review, degrading to summary-only if GitHub rejects the anchors."""
    try:
        return await _send(deps, ref, payload), len(payload.comments)
    except GitHubUnprocessable as exc:
        # The guard matters as much as the retry. A 422 caused by a stale
        # commit_id, a closed pull request, or a token without write access would
        # otherwise re-send an identical request and fail identically. One retry,
        # and only when there is something to remove.
        if not payload.comments:
            raise
        logger.warning(
            "github rejected %d inline anchors (%s); posting summary-only",
            len(payload.comments),
            exc,
        )
        return await _send(deps, ref, degrade(payload)), 0


async def _send(deps: JobDeps, ref: PullRequestRef, payload: ReviewPayload) -> int:
    return await deps.github.create_review(
        ref.repo_full_name,
        ref.pr_number,
        body=payload.body,
        comments=payload.comments,
        commit_id=payload.commit_id,
    )


async def _say(deps: JobDeps, ref: PullRequestRef, body: str) -> None:
    """Post a summary-only review, swallowing anything that goes wrong.

    Used for the one failure worth explaining on the pull request. It must never
    turn a handled problem into an unhandled one.
    """
    try:
        await deps.github.create_review(
            ref.repo_full_name, ref.pr_number, body=body, comments=[], commit_id=None
        )
    except GitHubError:
        logger.exception("could not post the explanation")
