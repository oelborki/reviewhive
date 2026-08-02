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
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from reviewhive.db.models import AgentCallRow, FindingRow, ReviewRow
from reviewhive.models import ReviewResult
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
        diff_text: str,
        diff_path: str | None = None,
    ) -> UUID:
        # Minted here rather than by the database so the caller holds the id
        # before the row is durable, which keeps this a single round trip and
        # lets a webhook return an id in the same breath as accepting the work.
        review_id = uuid4()
        encoded = diff_text.encode("utf-8")

        async with self._sessionmaker() as session:
            session.add(
                ReviewRow(
                    id=review_id,
                    status="pending",
                    source=source,
                    diff_sha256=hashlib.sha256(encoded).hexdigest(),
                    diff_bytes=len(encoded),
                    diff_path=diff_path,
                )
            )
            await session.commit()

        return review_id

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
                    skipped_files=result.skipped_files,
                    truncated_files=result.truncated_files,
                    # Priced now, not on read: the figure is what the run cost at
                    # the rates in force, and recomputing it later would rewrite
                    # history the next time a rate changed.
                    total_cost_usd=total_cost(result),
                    elapsed_ms=elapsed_ms,
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
    )
