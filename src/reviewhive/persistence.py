"""What a caller needs to record a review, and nothing about how.

This module imports `typing` and `reviewhive.models` — no SQLAlchemy, no driver.
That is deliberate: callers and the in-memory test double both depend on this
protocol, so the offline test suite never needs the `db` extra installed, and
`reviewhive.db` stays an implementation detail behind it.

The interface is two-phase on purpose. `start_review` records that a run began
and hands back its id; `finish_review` fills in what it produced. A one-shot
`save(result)` would be shorter and would not survive contact with a webhook,
which has to persist a row *before* the review runs so a request can be
acknowledged and the work picked up afterwards. One path, exercised from the
start, rather than two that drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID

from reviewhive.models import ReviewResult


@dataclass(frozen=True)
class GitHubRef:
    """Where a review came from, when it came from a pull request.

    One object rather than four optional parameters on `start_review`. Four
    separate `str | None` arguments make "three of the four set" a representable
    state, and it is not one — you either know which pull request this is or you
    do not. It also keeps `start_review` from growing a parameter every time a new
    source appears, and leaves the CLI's call site untouched.
    """

    repo_full_name: str
    pr_number: int
    head_sha: str
    delivery_id: str


@dataclass(frozen=True)
class ReviewRef:
    """Enough of an existing review to decide what to do about it.

    Deliberately not the ORM row. The store answers "does this exist, and how did
    it end?"; whether a previously failed review is worth retrying is the
    handler's call, and keeping the row out of its hands is what stops that
    decision leaking into the storage layer.
    """

    id: UUID
    status: str


@dataclass(frozen=True)
class StoredFinding:
    """A finding as it was posted, read back to answer a question about it.

    Carries the body as well as the headline: a rebuttal has to be judged against
    the original reasoning, and reconsidering a one-line title makes agreeing the
    path of least resistance.
    """

    ordinal: int
    file: str
    line: int | None
    severity: str
    title: str
    body: str


class DuplicateDelivery(Exception):
    """This delivery has already been recorded.

    Raised by the store instead of the driver's own integrity error, so a caller
    can treat a redelivery as a no-op without importing SQLAlchemy — and so a
    genuine constraint bug stays distinguishable from routine redelivery.
    """


@runtime_checkable
class ReviewStore(Protocol):
    """Somewhere a review run can be recorded.

    `runtime_checkable` so both the real store and the fake can be asserted
    against it. That only compares method names, which is exactly the drift worth
    catching between two implementations living in different files.
    """

    async def start_review(
        self,
        *,
        source: str,
        diff_text: str | None = None,
        diff_path: str | None = None,
        github: GitHubRef | None = None,
    ) -> UUID:
        """Record that a review has begun and return its id.

        Called *before* the review runs, so a crash mid-review leaves evidence
        rather than nothing. `diff_text` is hashed and measured, not stored.

        A webhook has no diff yet: it must acknowledge the delivery before making
        the slow fetch, so it passes `github` and no `diff_text`, then calls
        `mark_running` once the diff arrives. Raises `DuplicateDelivery` if this
        delivery has already been recorded.
        """
        ...

    async def mark_running(self, review_id: UUID, *, diff_text: str) -> None:
        """Record that the review is under way, and what it is reviewing.

        One write doing two things on purpose. The moment a run stops being
        merely accepted and starts actually reviewing is exactly the moment the
        diff exists, so there is never a window where the row claims a hash it
        does not have. It also makes a crash legible: `pending` means the process
        died before starting, `running` means it died mid-review — the only thing
        that separates a dead background task from a dispatch that never happened.
        """
        ...

    async def finish_review(
        self,
        review_id: UUID,
        result: ReviewResult,
        *,
        elapsed_ms: int,
    ) -> None:
        """Record what the run produced, and price it.

        Cost is computed here rather than read off the result, so the stored
        figure is a snapshot of the rates in force when the run happened.
        """
        ...

    async def fail_review(self, review_id: UUID, error: str) -> None:
        """Record that the run did not finish, and why."""
        ...

    async def record_posted_review(
        self,
        review_id: UUID,
        *,
        posted_review_id: int,
        comment_count: int,
    ) -> None:
        """Record that the review reached the pull request.

        Separate from `finish_review` because posting happens after persisting: a
        review that ran but could not be delivered is a succeeded review with no
        posted id, and conflating that with a failure would make `failed` mean two
        different things.
        """
        ...

    async def get_review(self, review_id: UUID) -> ReviewRef | None:
        """The status of one review.

        The webhook answers 202 with an id and nothing else, so without this the
        only way to see whether the background job finished is a SQL client.
        """
        ...

    async def latest_findings(
        self, *, repo_full_name: str, pr_number: int
    ) -> list[StoredFinding]:
        """What the most recent successful review of this pull request reported.

        The context a mention needs: a question is about these, and a rebuttal
        argues with one of them. Empty when nothing has been reviewed yet, which
        the caller should treat as "there is nothing to discuss" rather than as an
        error.
        """
        ...

    async def count_recent_mentions(
        self, *, repo_full_name: str, pr_number: int, within_seconds: int
    ) -> int:
        """How many mention runs this pull request has had lately.

        Anyone who can comment can spend money, and the self-login guard only stops
        the bot answering itself. This is what stops a person, or a loop nobody
        predicted, from doing it repeatedly.
        """
        ...

    async def find_review_by_delivery(self, delivery_id: str) -> ReviewRef | None:
        """The review recorded for this delivery, if any.

        The fast path for redelivery — a clean answer rather than a caught
        integrity error. It does not replace the unique constraint: two concurrent
        redeliveries both pass this check.
        """
        ...

    async def find_latest_review_for_head(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
        head_sha: str,
    ) -> ReviewRef | None:
        """The most recent review of this exact commit, if any.

        Different deliveries can describe the same code — a draft marked ready
        right after being opened is the common one. Reviewing it twice costs money
        and posts the same findings twice.
        """
        ...


class NullReviewStore:
    """A store for when there is no database.

    The CLI is the prompt-iteration loop and has to keep working with nothing
    running, so an unset `database_url` is a supported configuration rather than
    an error. Returning a real UUID keeps callers from having to branch on
    whether persistence is on.
    """

    async def start_review(
        self,
        *,
        source: str,
        diff_text: str | None = None,
        diff_path: str | None = None,
        github: GitHubRef | None = None,
    ) -> UUID:
        from uuid import uuid4

        return uuid4()

    async def mark_running(self, review_id: UUID, *, diff_text: str) -> None:
        return None

    async def finish_review(
        self,
        review_id: UUID,
        result: ReviewResult,
        *,
        elapsed_ms: int,
    ) -> None:
        return None

    async def fail_review(self, review_id: UUID, error: str) -> None:
        return None

    async def record_posted_review(
        self,
        review_id: UUID,
        *,
        posted_review_id: int,
        comment_count: int,
    ) -> None:
        return None

    async def get_review(self, review_id: UUID) -> ReviewRef | None:
        return None

    async def latest_findings(
        self, *, repo_full_name: str, pr_number: int
    ) -> list[StoredFinding]:
        # Nothing was stored, so nothing can be discussed. With no database a
        # mention can only become a review, which is the honest degradation.
        return []

    async def count_recent_mentions(
        self, *, repo_full_name: str, pr_number: int, within_seconds: int
    ) -> int:
        return 0

    async def find_review_by_delivery(self, delivery_id: str) -> ReviewRef | None:
        # Nothing was stored, so nothing is a duplicate. A webhook run with no
        # database re-reviews on every delivery, which is the honest behaviour:
        # idempotency here is a property of the store, not of the handler.
        return None

    async def find_latest_review_for_head(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
        head_sha: str,
    ) -> ReviewRef | None:
        return None
