"""In-memory stand-ins for things that would otherwise need infrastructure.

Separate from `stubs.py`, which fakes the Anthropic client. This is the storage
side, and it exists so the unit suite can assert what a caller *recorded* without
a database running — the same reason `ReviewStore` lives outside `reviewhive.db`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID, uuid4

from reviewhive.models import ReviewResult
from reviewhive.persistence import DuplicateDelivery, GitHubRef, ReviewRef
from reviewhive.pricing import total_cost


@dataclass
class SavedReview:
    """One recorded run, in the shape a test wants to assert against."""

    id: UUID
    source: str
    diff_sha256: str | None
    diff_bytes: int | None
    diff_path: str | None
    status: str = "pending"
    result: ReviewResult | None = None
    total_cost_usd: Decimal | None = None
    elapsed_ms: int | None = None
    error: str | None = None
    github: GitHubRef | None = None
    posted_review_id: int | None = None
    posted_comment_count: int | None = None
    focus: str | None = None


@dataclass
class InMemoryReviewStore:
    """A `ReviewStore` that keeps everything in a dict.

    Mirrors the real store's *observable* behaviour, not its implementation: the
    two-phase sequence, the diff being hashed rather than kept, and the cost being
    computed at finish time. Anything it lets through that Postgres would reject —
    an unknown status, a duplicate agent — is caught by the integration suite
    instead.
    """

    reviews: dict[UUID, SavedReview] = field(default_factory=dict)

    async def start_review(
        self,
        *,
        source: str,
        diff_text: str | None = None,
        diff_path: str | None = None,
        github: GitHubRef | None = None,
    ) -> UUID:
        if github is not None and any(
            saved.github and saved.github.delivery_id == github.delivery_id
            for saved in self.reviews.values()
        ):
            # Stands in for the unique constraint. Without it the fake would
            # accept a replay the real store rejects, and the handler's
            # duplicate-delivery path would go untested offline.
            raise DuplicateDelivery(github.delivery_id)

        review_id = uuid4()
        encoded = diff_text.encode("utf-8") if diff_text is not None else None
        self.reviews[review_id] = SavedReview(
            id=review_id,
            source=source,
            diff_sha256=hashlib.sha256(encoded).hexdigest() if encoded else None,
            diff_bytes=len(encoded) if encoded is not None else None,
            diff_path=diff_path,
            github=github,
        )
        return review_id

    async def mark_running(self, review_id: UUID, *, diff_text: str) -> None:
        encoded = diff_text.encode("utf-8")
        saved = self.reviews[review_id]
        saved.status = "running"
        saved.diff_sha256 = hashlib.sha256(encoded).hexdigest()
        saved.diff_bytes = len(encoded)

    async def record_posted_review(
        self,
        review_id: UUID,
        *,
        posted_review_id: int,
        comment_count: int,
    ) -> None:
        saved = self.reviews[review_id]
        saved.posted_review_id = posted_review_id
        saved.posted_comment_count = comment_count

    async def get_review(self, review_id: UUID) -> ReviewRef | None:
        saved = self.reviews.get(review_id)
        return ReviewRef(id=saved.id, status=saved.status) if saved else None

    async def find_review_by_delivery(self, delivery_id: str) -> ReviewRef | None:
        for saved in self.reviews.values():
            if saved.github and saved.github.delivery_id == delivery_id:
                return ReviewRef(id=saved.id, status=saved.status)
        return None

    async def find_latest_review_for_head(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
        head_sha: str,
    ) -> ReviewRef | None:
        matches = [
            saved
            for saved in self.reviews.values()
            if saved.github
            and saved.github.repo_full_name == repo_full_name
            and saved.github.pr_number == pr_number
            and saved.github.head_sha == head_sha
        ]
        return ReviewRef(id=matches[-1].id, status=matches[-1].status) if matches else None

    async def finish_review(
        self,
        review_id: UUID,
        result: ReviewResult,
        *,
        elapsed_ms: int,
    ) -> None:
        saved = self.reviews[review_id]
        saved.status = "succeeded"
        saved.result = result
        saved.total_cost_usd = total_cost(result)
        saved.elapsed_ms = elapsed_ms
        saved.focus = result.focus

    async def fail_review(self, review_id: UUID, error: str) -> None:
        saved = self.reviews[review_id]
        saved.status = "failed"
        saved.error = error

    @property
    def only(self) -> SavedReview:
        """The single recorded review, asserting there is exactly one."""
        assert len(self.reviews) == 1, f"expected one review, found {len(self.reviews)}"
        return next(iter(self.reviews.values()))
