"""The repository against a real Postgres.

What the in-memory fake cannot check: that the schema accepts what the store
writes, that the constraints reject what they should, and that cost-per-review is
actually queryable — which is the goal this phase exists to reach.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from reviewhive.db.models import AgentCallRow, FindingRow, ReviewRow
from reviewhive.models import AgentCall, MergedFinding, ReviewResult
from reviewhive.persistence import DuplicateDelivery, GitHubRef

# Declared per module: a conftest-level pytestmark is silently ignored.
pytestmark = pytest.mark.db

DIFF = "diff --git a/src/app/auth.py b/src/app/auth.py\n@@ -1 +1 @@\n+bad = 1\n"


def call(**kwargs) -> AgentCall:
    return AgentCall(
        agent=kwargs.pop("agent", "security"),
        model=kwargs.pop("model", "claude-haiku-4-5"),
        input_tokens=kwargs.pop("input_tokens", 1000),
        output_tokens=kwargs.pop("output_tokens", 200),
        **kwargs,
    )


def finding(**kwargs) -> MergedFinding:
    return MergedFinding(
        file=kwargs.pop("file", "src/app/auth.py"),
        line=kwargs.pop("line", 13),
        severity=kwargs.pop("severity", "high"),
        category=kwargs.pop("category", "sql-injection"),
        title=kwargs.pop("title", "SQL built by concatenation"),
        body=kwargs.pop("body", "Parameterise the query."),
        confidence=kwargs.pop("confidence", 0.9),
        sources=kwargs.pop("sources", ["security"]),
    )


def full_result() -> ReviewResult:
    return ReviewResult(
        findings=[
            finding(),
            finding(severity="medium", title="Unclear name", category="naming", line=20),
        ],
        suppressed_count=3,
        skipped_files=["package-lock.json (lockfile)"],
        truncated_files=["big.py (312 lines omitted)"],
        calls=[
            call(agent="security"),
            call(agent="style", output_tokens=400),
            call(agent="architecture", input_tokens=900, output_tokens=100),
        ],
    )


class TestRoundTrip:
    async def test_a_finished_review_reads_back_whole(self, store, engine) -> None:
        review_id = await store.start_review(source="cli", diff_text=DIFF, diff_path="pr.diff")
        await store.finish_review(review_id, full_result(), elapsed_ms=5400)

        async with engine.connect() as conn:
            row = (await conn.execute(select(ReviewRow).where(ReviewRow.id == review_id))).one()

        assert row.status == "succeeded"
        assert row.source == "cli"
        assert row.diff_path == "pr.diff"
        assert row.suppressed_count == 3
        assert row.elapsed_ms == 5400
        assert row.finished_at is not None
        assert row.skipped_files == ["package-lock.json (lockfile)"]
        assert row.truncated_files == ["big.py (312 lines omitted)"]

    async def test_findings_keep_their_posted_order(self, store, engine) -> None:
        """`ordinal` is the only identity a finding has — they are ranked and have
        no natural key, so the order they were shown in has to survive."""
        review_id = await store.start_review(source="cli", diff_text=DIFF)
        await store.finish_review(review_id, full_result(), elapsed_ms=1)

        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(FindingRow.ordinal, FindingRow.title)
                    .where(FindingRow.review_id == review_id)
                    .order_by(FindingRow.ordinal)
                )
            ).all()

        assert [r.ordinal for r in rows] == [0, 1]
        assert rows[0].title == "SQL built by concatenation"

    async def test_sources_survive_as_a_queryable_array(self, store, engine) -> None:
        """Stored as a Postgres array rather than JSON so provenance is a real
        query: which findings did the style reviewer raise?"""
        review_id = await store.start_review(source="cli", diff_text=DIFF)
        await store.finish_review(
            review_id,
            ReviewResult(findings=[finding(sources=["security", "style"])], calls=[call()]),
            elapsed_ms=1,
        )

        async with engine.connect() as conn:
            count = await conn.scalar(
                select(func.count())
                .select_from(FindingRow)
                .where(text("'style' = ANY(sources)"))
            )

        assert count == 1


class TestCostPerReview:
    """The phase gate: cost has to be answerable in SQL, and has to add up."""

    async def test_the_stored_total_equals_the_sum_of_its_calls(self, store, engine) -> None:
        review_id = await store.start_review(source="cli", diff_text=DIFF)
        await store.finish_review(review_id, full_result(), elapsed_ms=1)

        async with engine.connect() as conn:
            stored = await conn.scalar(
                select(ReviewRow.total_cost_usd).where(ReviewRow.id == review_id)
            )
            summed = await conn.scalar(
                select(func.sum(AgentCallRow.cost_usd)).where(AgentCallRow.review_id == review_id)
            )

        assert stored == summed

    async def test_a_known_token_count_produces_a_known_figure(self, store, engine) -> None:
        """1000 in @ $1/MTok + 200 out @ $5/MTok = $0.002, and money is exact."""
        review_id = await store.start_review(source="cli", diff_text=DIFF)
        await store.finish_review(review_id, ReviewResult(calls=[call()]), elapsed_ms=1)

        async with engine.connect() as conn:
            stored = await conn.scalar(
                select(ReviewRow.total_cost_usd).where(ReviewRow.id == review_id)
            )

        assert stored == Decimal("0.00200000")

    async def test_an_unpriced_model_is_null_not_zero(self, store, engine) -> None:
        """A missing price must stay distinguishable from a free call: SUM skips
        NULL, and `WHERE cost_usd IS NULL` finds every gap."""
        review_id = await store.start_review(source="cli", diff_text=DIFF)
        await store.finish_review(
            review_id,
            ReviewResult(calls=[call(), call(agent="style", model="claude-imaginary-9")]),
            elapsed_ms=1,
        )

        async with engine.connect() as conn:
            unpriced = (
                await conn.execute(
                    select(AgentCallRow.agent).where(AgentCallRow.cost_usd.is_(None))
                )
            ).scalars().all()
            stored = await conn.scalar(
                select(ReviewRow.total_cost_usd).where(ReviewRow.id == review_id)
            )

        assert unpriced == ["style"]
        assert stored == Decimal("0.00200000"), "the run total counts only priced calls"


class TestLifecycle:
    async def test_a_started_review_is_pending_with_no_finish_time(self, store, engine) -> None:
        """Phase 3's contract, proven a phase early: the row exists before the work
        does, so a crash mid-review leaves evidence rather than nothing."""
        review_id = await store.start_review(source="cli", diff_text=DIFF)

        async with engine.connect() as conn:
            row = (await conn.execute(select(ReviewRow).where(ReviewRow.id == review_id))).one()

        assert row.status == "pending"
        assert row.finished_at is None
        assert row.total_cost_usd == Decimal(0)

    async def test_a_failed_review_keeps_its_error(self, store, engine) -> None:
        review_id = await store.start_review(source="cli", diff_text=DIFF)
        await store.fail_review(review_id, "APIStatusError: 529 overloaded")

        async with engine.connect() as conn:
            row = (await conn.execute(select(ReviewRow).where(ReviewRow.id == review_id))).one()

        assert row.status == "failed"
        assert "529" in row.error
        assert row.finished_at is not None

    async def test_an_errored_agent_is_recorded_with_its_message(self, store, engine) -> None:
        """One agent failing is not the review failing — two working agents still
        produce a review, and the third's failure has to be visible afterwards."""
        review_id = await store.start_review(source="cli", diff_text=DIFF)
        await store.finish_review(
            review_id,
            ReviewResult(
                calls=[
                    call(),
                    call(agent="style", input_tokens=0, output_tokens=0, error="timeout"),
                ]
            ),
            elapsed_ms=1,
        )

        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    select(AgentCallRow).where(
                        AgentCallRow.review_id == review_id, AgentCallRow.agent == "style"
                    )
                )
            ).one()

        assert row.error == "timeout"
        assert row.cost_usd == Decimal(0), "no tokens, but priced rather than unknown"


class TestPullRequestReviews:
    """The webhook's write path, which the fake cannot check: the schema has to
    accept a row with no diff, and the constraint has to reject a replay."""

    def ref(self, **overrides) -> GitHubRef:
        base = {
            "repo_full_name": "oelborki/reviewhive-demo",
            "pr_number": 1,
            "head_sha": "a" * 40,
            "delivery_id": "f51b2160-8ea5-11f1-802a-ba3289ad09b9",
        }
        return GitHubRef(**{**base, **overrides})

    async def test_a_review_can_be_recorded_before_its_diff_exists(
        self, store, engine
    ) -> None:
        """The whole reason the hash columns became nullable: a delivery has to be
        acknowledged before the slow fetch runs."""
        review_id = await store.start_review(source="webhook", github=self.ref())

        async with engine.connect() as conn:
            row = (await conn.execute(select(ReviewRow).where(ReviewRow.id == review_id))).one()

        assert row.status == "pending"
        assert row.diff_sha256 is None
        assert row.diff_bytes is None
        assert row.repo_full_name == "oelborki/reviewhive-demo"
        assert row.pr_number == 1

    async def test_marking_running_attaches_the_diff(self, store, engine) -> None:
        """Status and hash move in one write, so the row is never `running` while
        claiming a diff it does not have."""
        review_id = await store.start_review(source="webhook", github=self.ref())
        await store.mark_running(review_id, diff_text=DIFF)

        async with engine.connect() as conn:
            row = (await conn.execute(select(ReviewRow).where(ReviewRow.id == review_id))).one()

        assert row.status == "running"
        assert row.diff_sha256 == hashlib.sha256(DIFF.encode()).hexdigest()
        assert row.diff_bytes == len(DIFF.encode())

    async def test_the_same_delivery_cannot_be_recorded_twice(self, store) -> None:
        """The guarantee the handler's lookup cannot give. Two concurrent
        redeliveries both pass a SELECT; only this stops the second one."""
        await store.start_review(source="webhook", github=self.ref())

        with pytest.raises(DuplicateDelivery):
            await store.start_review(source="webhook", github=self.ref())

    async def test_a_different_delivery_for_the_same_head_is_allowed(self, store) -> None:
        """Only the delivery is unique. A retry after a failed review has to stay
        possible for the same commit."""
        await store.start_review(source="webhook", github=self.ref())
        second = await store.start_review(source="webhook", github=self.ref(delivery_id="other"))

        assert second is not None

    async def test_cli_rows_do_not_collide_on_a_null_delivery(self, store) -> None:
        """Postgres ignores NULLs in a unique index, which is what lets every
        existing CLI row coexist with the constraint."""
        await store.start_review(source="cli", diff_text=DIFF)
        await store.start_review(source="cli", diff_text=DIFF)

    async def test_a_delivery_can_be_looked_up(self, store) -> None:
        review_id = await store.start_review(source="webhook", github=self.ref())

        found = await store.find_review_by_delivery(self.ref().delivery_id)

        assert found is not None
        assert found.id == review_id
        assert found.status == "pending"

    async def test_an_unknown_delivery_is_none(self, store) -> None:
        assert await store.find_review_by_delivery("never-seen") is None

    async def test_the_latest_review_for_a_head_is_the_most_recent(self, store) -> None:
        """`opened` followed immediately by `ready_for_review` is the real case,
        and reviewing the same commit twice costs money and posts twice."""
        await store.start_review(source="webhook", github=self.ref(delivery_id="first"))
        second = await store.start_review(source="webhook", github=self.ref(delivery_id="second"))

        found = await store.find_latest_review_for_head(
            repo_full_name="oelborki/reviewhive-demo", pr_number=1, head_sha="a" * 40
        )

        assert found is not None
        assert found.id == second

    async def test_a_different_head_is_not_matched(self, store) -> None:
        await store.start_review(source="webhook", github=self.ref())

        assert (
            await store.find_latest_review_for_head(
                repo_full_name="oelborki/reviewhive-demo", pr_number=1, head_sha="b" * 40
            )
            is None
        )

    async def test_posting_is_recorded_without_touching_status(self, store, engine) -> None:
        """A failed post is not a failed review. Conflating them would make
        `failed` mean two different things."""
        review_id = await store.start_review(source="webhook", github=self.ref())
        await store.mark_running(review_id, diff_text=DIFF)
        await store.finish_review(review_id, full_result(), elapsed_ms=1000)
        await store.record_posted_review(review_id, posted_review_id=4839508909, comment_count=2)

        async with engine.connect() as conn:
            row = (await conn.execute(select(ReviewRow).where(ReviewRow.id == review_id))).one()

        assert row.status == "succeeded"
        assert row.posted_review_id == 4839508909
        assert row.posted_comment_count == 2

    async def test_a_review_that_never_reached_github_is_findable(self, store, engine) -> None:
        """`posted_review_id IS NULL AND status = 'succeeded'` is the query for
        work that was paid for and never delivered."""
        review_id = await store.start_review(source="webhook", github=self.ref())
        await store.mark_running(review_id, diff_text=DIFF)
        await store.finish_review(review_id, full_result(), elapsed_ms=1000)

        async with engine.connect() as conn:
            undelivered = (
                await conn.execute(
                    select(func.count())
                    .select_from(ReviewRow)
                    .where(ReviewRow.status == "succeeded", ReviewRow.posted_review_id.is_(None))
                )
            ).scalar_one()

        assert undelivered == 1


class TestConstraints:
    async def test_children_go_when_the_review_goes(self, store, engine) -> None:
        review_id = await store.start_review(source="cli", diff_text=DIFF)
        await store.finish_review(review_id, full_result(), elapsed_ms=1)

        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM reviews WHERE id = :id"), {"id": review_id})

        async with engine.connect() as conn:
            calls = await conn.scalar(select(func.count()).select_from(AgentCallRow))
            findings = await conn.scalar(select(func.count()).select_from(FindingRow))

        assert (calls, findings) == (0, 0)

    async def test_one_call_per_agent_per_review_is_enforced(self, store, engine) -> None:
        """The invariant that turns a double-write into a loud error instead of a
        silently doubled cost figure."""
        review_id = await store.start_review(source="cli", diff_text=DIFF)
        await store.finish_review(review_id, ReviewResult(calls=[call()]), elapsed_ms=1)

        with pytest.raises(IntegrityError):
            await store.finish_review(review_id, ReviewResult(calls=[call()]), elapsed_ms=1)

    async def test_an_unknown_status_is_rejected(self, store, engine) -> None:
        """The CHECK constraint and the domain `Literal` are two copies of one
        closed set; this is the half the database enforces."""
        review_id = await store.start_review(source="cli", diff_text=DIFF)

        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE reviews SET status = 'elsewhere' WHERE id = :id"),
                    {"id": review_id},
                )
