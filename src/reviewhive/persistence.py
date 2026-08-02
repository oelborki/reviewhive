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

from typing import Protocol, runtime_checkable
from uuid import UUID

from reviewhive.models import ReviewResult


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
        diff_text: str,
        diff_path: str | None = None,
    ) -> UUID:
        """Record that a review has begun and return its id.

        Called *before* the review runs, so a crash mid-review leaves evidence
        rather than nothing. `diff_text` is hashed and measured, not stored.
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
        diff_text: str,
        diff_path: str | None = None,
    ) -> UUID:
        from uuid import uuid4

        return uuid4()

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
