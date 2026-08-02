from __future__ import annotations

from decimal import Decimal

import pytest
from tests.fakes import InMemoryReviewStore

from reviewhive.models import AgentCall, MergedFinding, ReviewResult
from reviewhive.persistence import NullReviewStore, ReviewStore

DIFF = "diff --git a/f.py b/f.py\n"


def result(**kwargs) -> ReviewResult:
    return ReviewResult(
        calls=kwargs.pop(
            "calls",
            [
                AgentCall(
                    agent="security",
                    model="claude-haiku-4-5",
                    input_tokens=1000,
                    output_tokens=200,
                )
            ],
        ),
        **kwargs,
    )


def finding(**kwargs) -> MergedFinding:
    return MergedFinding(
        file=kwargs.pop("file", "src/app/auth.py"),
        line=kwargs.pop("line", 13),
        severity=kwargs.pop("severity", "high"),
        category=kwargs.pop("category", "sql-injection"),
        title=kwargs.pop("title", "SQL built by concatenation"),
        body=kwargs.pop("body", "Parameterise it."),
        confidence=kwargs.pop("confidence", 0.9),
        sources=kwargs.pop("sources", ["security"]),
    )


@pytest.mark.parametrize("store", [InMemoryReviewStore(), NullReviewStore()])
def test_both_stores_satisfy_the_protocol(store) -> None:
    """The fake and the real store live in different files and will drift. The
    protocol is runtime-checkable so that drift is a failing test rather than an
    AttributeError during a review."""
    assert isinstance(store, ReviewStore)


def test_the_sql_store_satisfies_the_protocol() -> None:
    pytest.importorskip("sqlalchemy", reason="requires the db extra")
    from reviewhive.db.repository import SqlReviewStore

    assert isinstance(SqlReviewStore(None), ReviewStore)


class TestStartReview:
    async def test_a_started_review_is_pending_and_unfinished(self) -> None:
        """The state a webhook depends on: the row exists before the work does, so
        a crash mid-review leaves evidence rather than nothing."""
        store = InMemoryReviewStore()
        await store.start_review(source="cli", diff_text=DIFF)

        assert store.only.status == "pending"
        assert store.only.result is None

    async def test_the_diff_is_hashed_and_measured_not_kept(self) -> None:
        store = InMemoryReviewStore()
        await store.start_review(source="cli", diff_text=DIFF)

        assert len(store.only.diff_sha256) == 64
        assert store.only.diff_bytes == len(DIFF.encode())

    async def test_the_same_diff_hashes_the_same_way_twice(self) -> None:
        """What makes 'have we reviewed this before?' answerable without storing
        anyone's source."""
        store = InMemoryReviewStore()
        first = await store.start_review(source="cli", diff_text=DIFF)
        second = await store.start_review(source="cli", diff_text=DIFF)

        assert first != second
        assert store.reviews[first].diff_sha256 == store.reviews[second].diff_sha256


class TestFinishReview:
    async def test_finishing_records_the_result_and_the_cost(self) -> None:
        store = InMemoryReviewStore()
        review_id = await store.start_review(source="cli", diff_text=DIFF)
        await store.finish_review(review_id, result(), elapsed_ms=4200)

        saved = store.only
        assert saved.status == "succeeded"
        assert saved.elapsed_ms == 4200
        # 1000 in @ $1/MTok + 200 out @ $5/MTok
        assert saved.total_cost_usd == Decimal("0.002")

    async def test_cost_is_computed_at_finish_not_read_off_the_result(self) -> None:
        """`ReviewResult` carries no cost — deliberately, so a price list stays out
        of the domain model. The store is where the figure is fixed, which makes it
        a snapshot of the rates in force when the run happened."""
        store = InMemoryReviewStore()
        review_id = await store.start_review(source="cli", diff_text=DIFF)
        await store.finish_review(review_id, result(), elapsed_ms=1)

        assert not hasattr(ReviewResult, "total_cost_usd")
        assert store.only.total_cost_usd is not None

    async def test_a_review_with_no_findings_still_records(self) -> None:
        """A clean diff is a real outcome, not an absence of one."""
        store = InMemoryReviewStore()
        review_id = await store.start_review(source="cli", diff_text=DIFF)
        await store.finish_review(review_id, result(findings=[]), elapsed_ms=1)

        assert store.only.status == "succeeded"
        assert store.only.result.findings == []

    async def test_suppressed_findings_are_counted_not_stored(self) -> None:
        """Only posted findings survive `rank_and_cut`; the rest are a number by
        the time anything can persist them."""
        store = InMemoryReviewStore()
        review_id = await store.start_review(source="cli", diff_text=DIFF)
        await store.finish_review(
            review_id, result(findings=[finding()], suppressed_count=7), elapsed_ms=1
        )

        assert len(store.only.result.findings) == 1
        assert store.only.result.suppressed_count == 7


class TestFailReview:
    async def test_a_failed_review_keeps_its_error(self) -> None:
        store = InMemoryReviewStore()
        review_id = await store.start_review(source="cli", diff_text=DIFF)
        await store.fail_review(review_id, "APIStatusError: 529")

        assert store.only.status == "failed"
        assert store.only.error == "APIStatusError: 529"
        assert store.only.result is None


class TestNullStore:
    async def test_it_records_nothing_but_still_returns_an_id(self) -> None:
        """Callers should not have to branch on whether persistence is configured."""
        store = NullReviewStore()
        review_id = await store.start_review(source="cli", diff_text=DIFF)

        assert review_id is not None
        await store.finish_review(review_id, result(), elapsed_ms=1)
        await store.fail_review(review_id, "boom")
