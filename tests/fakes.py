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
from reviewhive.pricing import total_cost


@dataclass
class SavedReview:
    """One recorded run, in the shape a test wants to assert against."""

    id: UUID
    source: str
    diff_sha256: str
    diff_bytes: int
    diff_path: str | None
    status: str = "pending"
    result: ReviewResult | None = None
    total_cost_usd: Decimal | None = None
    elapsed_ms: int | None = None
    error: str | None = None


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
        diff_text: str,
        diff_path: str | None = None,
    ) -> UUID:
        review_id = uuid4()
        encoded = diff_text.encode("utf-8")
        self.reviews[review_id] = SavedReview(
            id=review_id,
            source=source,
            diff_sha256=hashlib.sha256(encoded).hexdigest(),
            diff_bytes=len(encoded),
            diff_path=diff_path,
        )
        return review_id

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

    async def fail_review(self, review_id: UUID, error: str) -> None:
        saved = self.reviews[review_id]
        saved.status = "failed"
        saved.error = error

    @property
    def only(self) -> SavedReview:
        """The single recorded review, asserting there is exactly one."""
        assert len(self.reviews) == 1, f"expected one review, found {len(self.reviews)}"
        return next(iter(self.reviews.values()))
