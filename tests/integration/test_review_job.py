"""The whole webhook path except the endpoint, entirely offline.

The real graph around a stub Anthropic client, the real store protocol around an
in-memory one, and the real HTTP client around a stub transport. Nothing is
patched, because every one of those is a constructor argument.

No `importorskip`: `jobs.py` imports neither FastAPI nor SQLAlchemy, which is why
it lives at the top level rather than under `api/`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from tests.fakes import InMemoryReviewStore
from tests.github_stubs import make_transport
from tests.stubs import StubAnthropic, finding

from reviewhive.config import Settings
from reviewhive.github.client import GitHubClient
from reviewhive.graph.build import build_review_graph
from reviewhive.jobs import JobDeps, PullRequestRef, review_pull_request
from reviewhive.persistence import GitHubRef

REF = PullRequestRef(repo_full_name="oelborki/reviewhive-demo", pr_number=1, head_sha="a" * 40)
GH_REF = GitHubRef(
    repo_full_name=REF.repo_full_name,
    pr_number=REF.pr_number,
    head_sha=REF.head_sha,
    delivery_id="delivery-1",
)


@pytest.fixture
def settings() -> Settings:
    return Settings(anthropic_api_key="test")


def github_handler(
    *,
    diff: str,
    review_status: int = 200,
    review_statuses: list[int] | None = None,
):
    """Serves the diff, then answers review posts from `review_statuses` in turn."""
    remaining = list(review_statuses or [review_status])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/reviews"):
            status = remaining.pop(0) if remaining else 200
            if status == 422:
                return httpx.Response(422, json={"message": "Validation Failed", "errors": []})
            return httpx.Response(status, json={"id": 4839508909})
        return httpx.Response(200, text=diff)

    return handler


async def run_job(diff_text, settings, *, handler, responses=None, store=None):
    """Assemble the real objects around the stubs and run one job."""
    client = StubAnthropic(responses=responses or {"security": [finding()]})
    transport, seen = make_transport(handler)
    github = GitHubClient(token="t", transport=transport)
    store = store or InMemoryReviewStore()

    review_id = await store.start_review(source="webhook", github=GH_REF)
    deps = JobDeps(
        settings=settings,
        graph=build_review_graph(client, settings),
        github=github,
        store=store,
    )
    await review_pull_request(deps, REF, review_id)
    return store, seen, review_id


class TestHappyPath:
    async def test_the_review_is_recorded_and_posted(self, diff_text, settings) -> None:
        store, seen, review_id = await run_job(
            diff_text, settings, handler=github_handler(diff=diff_text)
        )

        saved = store.reviews[review_id]
        assert saved.status == "succeeded"
        assert saved.posted_review_id == 4839508909
        assert [r.url.path for r in seen] == [
            "/repos/oelborki/reviewhive-demo/pulls/1",
            "/repos/oelborki/reviewhive-demo/pulls/1/reviews",
        ]

    async def test_the_diff_is_attached_when_it_arrives(self, diff_text, settings) -> None:
        """`mark_running` is what fills the hash in. A row that stayed `pending`
        with no diff would mean the job never really started."""
        store, _, review_id = await run_job(
            diff_text, settings, handler=github_handler(diff=diff_text)
        )

        assert store.reviews[review_id].diff_sha256 is not None
        assert store.reviews[review_id].diff_bytes == len(diff_text.encode())

    async def test_the_posted_review_carries_the_head_sha(self, diff_text, settings) -> None:
        """Without commit_id GitHub anchors to 'latest', so a pull request that
        moved between the fetch and the post silently re-points every comment."""
        _, seen, _ = await run_job(diff_text, settings, handler=github_handler(diff=diff_text))

        assert json.loads(seen[-1].content)["commit_id"] == REF.head_sha

    async def test_findings_reach_the_posted_body(self, diff_text, settings) -> None:
        _, seen, _ = await run_job(
            diff_text,
            settings,
            handler=github_handler(diff=diff_text),
            responses={"security": [finding(title="Hardcoded credential")]},
        )

        assert "Hardcoded credential" in json.loads(seen[-1].content)["body"]


class TestDegradation:
    async def test_a_rejected_anchor_becomes_a_summary_only_review(
        self, diff_text, settings
    ) -> None:
        """The behaviour the phase gate checks by hand. GitHub rejects the whole
        request over one bad anchor, so the recovery is to drop them all rather
        than lose the review."""
        _, seen, _ = await run_job(
            diff_text,
            settings,
            handler=github_handler(diff=diff_text, review_statuses=[422, 200]),
        )

        posts = [r for r in seen if r.url.path.endswith("/reviews")]
        assert len(posts) == 2

        retry = json.loads(posts[1].content)
        assert retry["comments"] == []
        assert "commit_id" not in retry

    async def test_the_degraded_review_still_carries_every_finding(
        self, diff_text, settings
    ) -> None:
        _, seen, _ = await run_job(
            diff_text,
            settings,
            handler=github_handler(diff=diff_text, review_statuses=[422, 200]),
            responses={"security": [finding(title="Hardcoded credential")]},
        )

        body = json.loads([r for r in seen if r.url.path.endswith("/reviews")][1].content)["body"]
        assert "Hardcoded credential" in body
        assert "rejected the inline anchors" in body

    async def test_degradation_is_recorded_as_zero_inline_comments(
        self, diff_text, settings
    ) -> None:
        """The stored count is what answers 'did the fallback fire?' — it is not
        derivable from the findings, which all still have their lines."""
        store, _, review_id = await run_job(
            diff_text,
            settings,
            handler=github_handler(diff=diff_text, review_statuses=[422, 200]),
        )

        assert store.reviews[review_id].posted_comment_count == 0

    async def test_a_422_with_nothing_to_remove_is_not_retried(
        self, diff_text, settings
    ) -> None:
        """A 422 from a stale sha or a read-only token would otherwise re-send an
        identical request forever."""
        _, seen, _ = await run_job(
            diff_text,
            settings,
            handler=github_handler(diff=diff_text, review_statuses=[422, 422]),
            responses={"security": []},
        )

        posts = [r for r in seen if r.url.path.endswith("/reviews")]
        assert len(posts) == 1


class TestFailures:
    async def test_a_diff_too_large_is_explained_on_the_pull_request(
        self, diff_text, settings
    ) -> None:
        """Known, permanent and worth saying out loud. Coverage is disclosed even
        when the coverage is none."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/reviews"):
                return httpx.Response(200, json={"id": 1})
            return httpx.Response(406, json={"message": "too large"})

        store, seen, review_id = await run_job(diff_text, settings, handler=handler)

        assert store.reviews[review_id].status == "failed"
        assert "declined to serve the diff" in json.loads(seen[-1].content)["body"]

    async def test_an_ordinary_fetch_failure_says_nothing_on_the_pull_request(
        self, diff_text, settings
    ) -> None:
        """A bot commenting 'I errored' on someone's pull request is worse than a
        bot that stays quiet."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"message": "server error"})

        store, seen, review_id = await run_job(diff_text, settings, handler=handler)

        assert store.reviews[review_id].status == "failed"
        assert not [r for r in seen if r.url.path.endswith("/reviews")]

    async def test_a_failed_post_leaves_the_review_succeeded(
        self, diff_text, settings
    ) -> None:
        """It ran and produced findings; only delivery failed. Calling that a
        failed review would make `failed` mean two different things."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/reviews"):
                return httpx.Response(500, json={"message": "server error"})
            return httpx.Response(200, text=diff_text)

        store, _, review_id = await run_job(diff_text, settings, handler=handler)

        saved = store.reviews[review_id]
        assert saved.status == "succeeded"
        assert saved.posted_review_id is None

    async def test_the_job_never_raises(self, diff_text, settings) -> None:
        """A background task's exception lands in the server's logger and nowhere
        a human looks, so it has to be caught and recorded here."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("network is down")

        store, _, review_id = await run_job(diff_text, settings, handler=handler)

        assert store.reviews[review_id].status in {"failed", "pending"}

    async def test_an_unpostable_explanation_is_not_a_second_failure(
        self, diff_text, settings
    ) -> None:
        """The diff was too large *and* the explanation could not be posted. There
        is nothing further to try and nobody to tell; the row is already `failed`
        and must stay that way."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/reviews"):
                return httpx.Response(403, json={"message": "forbidden"})
            return httpx.Response(406, json={"message": "too large"})

        store, _, review_id = await run_job(diff_text, settings, handler=handler)

        assert store.reviews[review_id].status == "failed"


class TestStorageFailures:
    """The store failing must never cost the review.

    It took real money and real minutes. Losing the output in order to report a
    database problem is the wrong way round, so each of these is logged and
    stepped over rather than raised.
    """

    async def test_a_graph_failure_is_recorded_against_the_row(
        self, diff_text, settings
    ) -> None:
        """Distinct from an agent failing — `run_agent` swallows that one. This is
        the graph itself coming apart, which nothing below it can absorb."""
        store = InMemoryReviewStore()
        transport, _ = make_transport(github_handler(diff=diff_text))
        review_id = await store.start_review(source="webhook", github=GH_REF)

        deps = JobDeps(
            settings=settings,
            graph=SimpleNamespace(ainvoke=_raise_async),
            github=GitHubClient(token="t", transport=transport),
            store=store,
        )
        await review_pull_request(deps, REF, review_id)

        assert store.reviews[review_id].status == "failed"
        assert "graph is broken" in store.reviews[review_id].error

    async def test_a_review_that_cannot_be_saved_is_still_posted(
        self, diff_text, settings
    ) -> None:
        """The findings exist and the pull request is what a human reads. Losing
        the post as well would turn one problem into two."""
        store = InMemoryReviewStore()
        store.finish_review = _raise_async  # type: ignore[method-assign]

        _, seen, _ = await run_job(
            diff_text, settings, handler=github_handler(diff=diff_text), store=store
        )

        assert [r for r in seen if r.url.path.endswith("/reviews")]

    async def test_a_post_that_cannot_be_recorded_is_still_a_post(
        self, diff_text, settings
    ) -> None:
        """The review is on the pull request either way. Only the bookkeeping is
        lost, and raising here would make it look like the post failed."""
        store = InMemoryReviewStore()
        store.record_posted_review = _raise_async  # type: ignore[method-assign]

        _, seen, review_id = await run_job(
            diff_text, settings, handler=github_handler(diff=diff_text), store=store
        )

        assert [r for r in seen if r.url.path.endswith("/reviews")]
        assert store.reviews[review_id].posted_review_id is None


async def _raise_async(*_args, **_kwargs):
    raise RuntimeError("graph is broken")
