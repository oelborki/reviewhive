"""The only module that writes SQL.

Everything above this depends on the `ReviewStore` protocol, so swapping the
storage engine, or standing in a fake, touches nothing else.

The mapping between `ReviewResult` and the row types is written out by hand
rather than dumped. `model_dump()` would look tidier and would be wrong: it omits
nothing but also knows nothing, so the cost — which is not a field — would vanish
silently, and every future field added to a Pydantic model would land in the
database without anyone deciding it should.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from reviewhive.db.models import AgentCallRow, FindingRow, ReviewRow
from reviewhive.models import ReviewResult
from reviewhive.persistence import DuplicateDelivery, GitHubRef, ReviewRef, StoredFinding
from reviewhive.pricing import cost_of, total_cost


class SqlReviewStore:
    """Records reviews in Postgres.

    Holds the sessionmaker rather than a session, and opens one per method. A
    session is not safe to share across concurrent tasks, and a long-lived one
    would pin a connection for the lifetime of whatever holds this object.
    """

    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def start_review(
        self,
        *,
        source: str,
        diff_text: str | None = None,
        diff_path: str | None = None,
        github: GitHubRef | None = None,
    ) -> UUID:
        # Minted here rather than by the database so the caller holds the id
        # before the row is durable, which keeps this a single round trip and
        # lets a webhook return an id in the same breath as accepting the work.
        review_id = uuid4()
        encoded = diff_text.encode("utf-8") if diff_text is not None else None

        async with self._sessionmaker() as session:
            session.add(
                ReviewRow(
                    id=review_id,
                    status="pending",
                    source=source,
                    diff_sha256=hashlib.sha256(encoded).hexdigest() if encoded else None,
                    diff_bytes=len(encoded) if encoded is not None else None,
                    diff_path=diff_path,
                    repo_full_name=github.repo_full_name if github else None,
                    pr_number=github.pr_number if github else None,
                    head_sha=github.head_sha if github else None,
                    delivery_id=github.delivery_id if github else None,
                )
            )
            try:
                await session.commit()
            except IntegrityError as exc:
                # The unique constraint on delivery_id, reached when two
                # redeliveries race past the handler's lookup. Translated so the
                # caller never has to import a driver exception to no-op on a
                # replay — and so a real constraint bug stays distinguishable.
                await session.rollback()
                if github is None or "delivery_id" not in str(exc.orig):
                    raise
                raise DuplicateDelivery(github.delivery_id) from exc

        return review_id

    async def mark_running(self, review_id: UUID, *, diff_text: str) -> None:
        encoded = diff_text.encode("utf-8")
        async with self._sessionmaker() as session:
            await session.execute(
                update(ReviewRow)
                .where(ReviewRow.id == review_id)
                # Status and diff move together because they become true together:
                # the run starts reviewing at the moment it has something to
                # review, so the row is never `running` with an unknown diff.
                .values(
                    status="running",
                    diff_sha256=hashlib.sha256(encoded).hexdigest(),
                    diff_bytes=len(encoded),
                )
            )
            await session.commit()

    async def finish_review(
        self,
        review_id: UUID,
        result: ReviewResult,
        *,
        elapsed_ms: int,
    ) -> None:
        async with self._sessionmaker() as session:
            session.add_all(
                [_call_row(review_id, call) for call in result.calls]
                + [
                    _finding_row(review_id, ordinal, finding)
                    for ordinal, finding in enumerate(result.findings)
                ]
            )
            await session.execute(
                update(ReviewRow)
                .where(ReviewRow.id == review_id)
                .values(
                    status="succeeded",
                    suppressed_count=result.suppressed_count,
                    # Counted apart from suppression on purpose: one is a finding
                    # this review stands behind and had no room for, the other is a
                    # claim the critic judged wrong. A query for "how often is the
                    # critic deleting things" cannot be asked of a single total.
                    retracted_count=result.retracted_count,
                    skipped_files=result.skipped_files,
                    truncated_files=result.truncated_files,
                    # Priced now, not on read: the figure is what the run cost at
                    # the rates in force, and recomputing it later would rewrite
                    # history the next time a rate changed.
                    total_cost_usd=total_cost(result),
                    elapsed_ms=elapsed_ms,
                    # Mapped explicitly, like everything else here. A narrowed run
                    # is not comparable to a full one, so a cost or finding query
                    # that mixes them silently is answering a different question.
                    focus=result.focus,
                    finished_at=datetime.now(UTC),
                )
            )
            # Children and the parent update commit together, so a review is
            # never `succeeded` with its calls missing.
            await session.commit()

    async def fail_review(self, review_id: UUID, error: str) -> None:
        async with self._sessionmaker() as session:
            await session.execute(
                update(ReviewRow)
                .where(ReviewRow.id == review_id)
                .values(status="failed", error=error, finished_at=datetime.now(UTC))
            )
            await session.commit()

    async def record_posted_review(
        self,
        review_id: UUID,
        *,
        posted_review_id: int,
        comment_count: int,
    ) -> None:
        async with self._sessionmaker() as session:
            # Status is left alone. The review succeeded when it produced
            # findings; delivering them is a separate fact, and
            # `posted_review_id IS NULL AND status = 'succeeded'` is the query for
            # a review that ran but never reached anyone.
            await session.execute(
                update(ReviewRow)
                .where(ReviewRow.id == review_id)
                .values(
                    posted_review_id=posted_review_id,
                    posted_comment_count=comment_count,
                )
            )
            await session.commit()

    async def get_review(self, review_id: UUID) -> ReviewRef | None:
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(ReviewRow.id, ReviewRow.status).where(ReviewRow.id == review_id)
                )
            ).first()
        return ReviewRef(id=row.id, status=row.status) if row else None

    async def latest_findings(
        self, *, repo_full_name: str, pr_number: int
    ) -> list[StoredFinding]:
        async with self._sessionmaker() as session:
            # The newest *succeeded* review, not the newest row: a failed or still
            # running one has no findings, and a mention arriving mid-review should
            # discuss the last review that finished rather than nothing.
            latest = (
                select(ReviewRow.id)
                .where(
                    ReviewRow.repo_full_name == repo_full_name,
                    ReviewRow.pr_number == pr_number,
                    ReviewRow.status == "succeeded",
                    ReviewRow.source != "mention",
                )
                .order_by(ReviewRow.created_at.desc())
                .limit(1)
                .scalar_subquery()
            )
            rows = (
                await session.execute(
                    select(
                        FindingRow.ordinal,
                        FindingRow.file,
                        FindingRow.line,
                        FindingRow.severity,
                        FindingRow.title,
                        FindingRow.body,
                    )
                    .where(FindingRow.review_id == latest)
                    .order_by(FindingRow.ordinal)
                )
            ).all()

        return [
            StoredFinding(
                ordinal=row.ordinal,
                file=row.file,
                line=row.line,
                severity=row.severity,
                title=row.title,
                body=row.body,
            )
            for row in rows
        ]

    async def count_recent_mentions(
        self, *, repo_full_name: str, pr_number: int, within_seconds: int
    ) -> int:
        cutoff = datetime.now(UTC) - timedelta(seconds=within_seconds)
        async with self._sessionmaker() as session:
            return (
                await session.execute(
                    select(func.count())
                    .select_from(ReviewRow)
                    .where(
                        ReviewRow.repo_full_name == repo_full_name,
                        ReviewRow.pr_number == pr_number,
                        ReviewRow.source == "mention",
                        ReviewRow.created_at >= cutoff,
                    )
                )
            ).scalar_one()

    async def find_review_by_delivery(self, delivery_id: str) -> ReviewRef | None:
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(ReviewRow.id, ReviewRow.status).where(
                        ReviewRow.delivery_id == delivery_id
                    )
                )
            ).first()
        return ReviewRef(id=row.id, status=row.status) if row else None

    async def find_latest_review_for_head(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
        head_sha: str,
    ) -> ReviewRef | None:
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(ReviewRow.id, ReviewRow.status)
                    .where(
                        ReviewRow.repo_full_name == repo_full_name,
                        ReviewRow.pr_number == pr_number,
                        ReviewRow.head_sha == head_sha,
                    )
                    .order_by(ReviewRow.created_at.desc())
                    .limit(1)
                )
            ).first()
        return ReviewRef(id=row.id, status=row.status) if row else None


def _call_row(review_id: UUID, call) -> AgentCallRow:
    cost: Decimal | None = cost_of(call)
    return AgentCallRow(
        review_id=review_id,
        agent=call.agent,
        model=call.model,
        input_tokens=call.input_tokens,
        output_tokens=call.output_tokens,
        cache_read_tokens=call.cache_read_tokens,
        latency_ms=call.latency_ms,
        findings_returned=call.findings_returned,
        error=call.error,
        # None when the model is not in the price table, which stays
        # distinguishable from a call that genuinely cost nothing.
        cost_usd=cost,
    )


def _finding_row(review_id: UUID, ordinal: int, finding) -> FindingRow:
    return FindingRow(
        review_id=review_id,
        # Position in the posted list. These are ranked and have no natural key,
        # so the order they were shown in is the only stable identity they have.
        ordinal=ordinal,
        file=finding.file,
        line=finding.line,
        severity=finding.severity,
        category=finding.category,
        title=finding.title,
        body=finding.body,
        confidence=finding.confidence,
        sources=list(finding.sources),
        # Whether the critic pass rewrote this. Stored so the pass can be judged
        # against what it did to real reviews rather than against a probe score --
        # the same reason `sources` records the lane.
        amended=finding.amended,
    )
