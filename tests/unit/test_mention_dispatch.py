"""Dispatch when something goes wrong.

`test_mentions.py` drives these paths through the endpoint, which is the right
level for the happy ones. These are the failures — a lost reply, a diff that will
not fetch, an ordinal the model invented — and they are easier to state, and much
easier to read, by calling the functions directly.

No `importorskip`: `dispatch.py` imports neither FastAPI nor SQLAlchemy, which is
the property that keeps the whole mention path in the default offline suite.

The through-line is the module's own promise: *nothing here raises*. A mention is
a courtesy, and failing to answer one must not take the handler down or leave a
review half-recorded.
"""

from __future__ import annotations

import httpx
import pytest
from tests.fakes import InMemoryReviewStore
from tests.github_stubs import always, make_transport
from tests.stubs import StubAnthropic

from reviewhive.config import Settings
from reviewhive.github.client import GitHubClient
from reviewhive.graph.build import build_review_graph
from reviewhive.jobs import JobDeps, PullRequestRef
from reviewhive.mentions.dispatch import (
    _post_reply,
    _target,
    _thread_target,
    handle_mention,
    respond_to_mention,
)
from reviewhive.mentions.intent import CommentIntent, PriorFinding

REPO = "oelborki/reviewhive-demo"
REF = PullRequestRef(repo_full_name=REPO, pr_number=1, head_sha="a" * 40)


def prior(ordinal: int = 1, file: str = "app/db.py", line: int | None = 56) -> PriorFinding:
    return PriorFinding(
        ordinal=ordinal,
        file=file,
        line=line,
        severity="high",
        title="SQL injection in search_tasks",
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(
        anthropic_api_key="test",
        github_token="github_pat_test",
        github_webhook_secret="s3cret",
        allowed_repos=frozenset({REPO}),
    )


def make_deps(settings, handler, *, outputs=None, store=None) -> JobDeps:
    transport, _ = make_transport(handler)
    stub = StubAnthropic(outputs=outputs or {})
    return JobDeps(
        settings=settings,
        graph=build_review_graph(stub, settings),
        github=GitHubClient(token="t", transport=transport),
        store=store or InMemoryReviewStore(),
        client=stub,
        self_login="oelborki",
    )


class TestThreadTarget:
    """Which finding an inline reply belongs to, from the comment's own anchor."""

    def test_a_comment_with_no_path_targets_nothing(self) -> None:
        """A conversation comment has no file, so there is no thread to resolve."""
        assert _thread_target([prior()], None, None) is None

    def test_a_path_with_no_matching_finding_targets_nothing(self) -> None:
        assert _thread_target([prior(file="app/db.py")], "app/other.py", 56) is None

    def test_a_matching_path_and_line_resolves_the_ordinal(self) -> None:
        assert _thread_target([prior(ordinal=3)], "app/db.py", 56) == 3

    def test_a_path_alone_matches_when_the_line_is_unknown(self) -> None:
        """GitHub omits the line on an outdated comment; the file is still enough."""
        assert _thread_target([prior(ordinal=3)], "app/db.py", None) == 3


class TestTarget:
    """Which finding an intent points at."""

    def test_an_intent_naming_nothing_has_no_target(self) -> None:
        intent = CommentIntent(action="answer_question", rationale="r")
        assert _target(intent, [prior()]) is None

    def test_an_invented_ordinal_is_dropped_rather_than_trusted(self) -> None:
        """Answering about the wrong finding is worse than answering about none,
        so an ordinal with no finding behind it resolves to nothing."""
        intent = CommentIntent(action="reconsider", target_ordinal=99, rationale="r")
        assert _target(intent, [prior(ordinal=1)]) is None

    def test_a_real_ordinal_resolves(self) -> None:
        intent = CommentIntent(action="reconsider", target_ordinal=2, rationale="r")
        assert _target(intent, [prior(ordinal=2)]).ordinal == 2


class TestPostReply:
    async def test_a_reply_that_cannot_be_posted_is_logged_not_raised(
        self, settings
    ) -> None:
        """The answer cost money and is lost either way. Failing the run on top of
        that would also discard the recorded calls."""
        deps = make_deps(settings, always(httpx.Response(403, json={"message": "no"})))

        await _post_reply(deps, REF, "here is why", None)

    async def test_a_failed_threaded_reply_is_logged_not_raised(self, settings) -> None:
        deps = make_deps(settings, always(httpx.Response(422, json={"message": "no"})))

        await _post_reply(deps, REF, "here is why", 555)


class TestHandleMention:
    async def test_a_re_review_with_no_review_row_is_skipped(self, settings) -> None:
        """The row is written by the caller before the work starts. Without one
        there is nowhere to record the result, so spending three agent calls
        would produce a review nothing could store."""
        deps = make_deps(
            settings,
            always(httpx.Response(200, text="diff")),
            outputs={
                "CommentIntent": CommentIntent(action="full_review", rationale="asked again")
            },
        )

        outcome = await handle_mention(
            deps, REF, comment="@reviewhive take another look", findings=[], bodies={}
        )

        assert outcome.review_dispatched is False
        assert outcome.reply is None

    async def test_a_diff_that_will_not_fetch_produces_no_reply(self, settings) -> None:
        """Both remaining paths need the diff. Without it there is nothing to
        answer from, and inventing an answer is worse than staying quiet."""
        deps = make_deps(
            settings,
            always(httpx.Response(403, json={"message": "forbidden"})),
            outputs={
                "CommentIntent": CommentIntent(
                    action="answer_question", question="why?", rationale="a question"
                )
            },
        )

        outcome = await handle_mention(
            deps, REF, comment="@reviewhive why?", findings=[prior()], bodies={}
        )

        assert outcome.reply is None
        # The classifier still ran and still cost money, so it is still recorded.
        assert len(outcome.calls) == 1


class TestRespondToMention:
    async def test_an_unexpected_failure_marks_the_row_failed(self, settings) -> None:
        """The outer guard. Whatever goes wrong, the run must not be left
        `pending` forever — a row nobody finished is indistinguishable from one
        still in flight."""
        store = InMemoryReviewStore()
        review_id = await store.start_review(source="mention", diff_text="d")
        store.latest_findings = _raises  # type: ignore[method-assign]

        deps = make_deps(settings, always(httpx.Response(500, json={})), store=store)

        await respond_to_mention(deps, REF, review_id, comment="@reviewhive why?")

        assert store.reviews[review_id].status == "failed"
        assert "storage is down" in store.reviews[review_id].error

    async def test_a_failure_that_cannot_even_be_recorded_stays_quiet(
        self, settings
    ) -> None:
        """The last resort. If the store is the thing that is broken, recording
        the breakage will fail too — and a background task that raises here has
        nowhere useful to raise to."""
        store = InMemoryReviewStore()
        review_id = await store.start_review(source="mention", diff_text="d")
        store.latest_findings = _raises  # type: ignore[method-assign]
        store.fail_review = _raises  # type: ignore[method-assign]

        deps = make_deps(settings, always(httpx.Response(500, json={})), store=store)

        await respond_to_mention(deps, REF, review_id, comment="@reviewhive why?")


async def _raises(*_args, **_kwargs) -> None:
    raise RuntimeError("storage is down")
