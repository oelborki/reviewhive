"""Responding when the bot is mentioned.

The guard tests carry the weight here. Measurement against a real review showed
that two of the three filters the design assumed are useless: posting fifteen
inline comments fired fifteen `pull_request_review_comment` deliveries, each with
`sender.type == "User"` and `author_association == "OWNER"`, because the bot acts
as a token belonging to a person. The self-login comparison is the only thing
between one review and fifteen re-triggers.
"""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

pytest.importorskip("fastapi", reason="requires the service extra")

from fastapi.testclient import TestClient
from tests.fakes import InMemoryReviewStore
from tests.github_stubs import make_transport, signed_headers
from tests.stubs import StubAnthropic, finding

from reviewhive.api.app import create_app
from reviewhive.config import Settings
from reviewhive.github.client import GitHubClient
from reviewhive.graph.build import build_review_graph
from reviewhive.jobs import JobDeps
from reviewhive.mentions.intent import CommentIntent
from reviewhive.mentions.respond import Answer, Verdict
from reviewhive.models import AgentCall, MergedFinding, ReviewResult
from reviewhive.persistence import GitHubRef

SECRET = "911cdf77b9ee3ae9472fde9db672d84c"
REPO = "oelborki/reviewhive-demo"
SELF = "oelborki"


def comment_payload(
    *,
    body: str = "@reviewhive why is this a problem?",
    login: str = "someone-else",
    association: str = "OWNER",
    user_type: str = "User",
    action: str = "created",
    is_pr: bool = True,
    comment_id: int = 555,
    path: str | None = None,
    line: int | None = None,
) -> bytes:
    comment = {
        "id": comment_id,
        "body": body,
        "author_association": association,
        "user": {"login": login, "type": user_type},
    }
    if path:
        comment["path"] = path
        comment["line"] = line
    issue = {"number": 1}
    if is_pr:
        issue["pull_request"] = {"url": f"https://api.github.com/repos/{REPO}/pulls/1"}
    return json.dumps(
        {
            "action": action,
            "issue": issue,
            "comment": comment,
            "repository": {"full_name": REPO},
            "sender": {"login": login, "type": user_type},
        }
    ).encode()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        anthropic_api_key="test",
        github_token="github_pat_test",
        github_webhook_secret=SECRET,
        allowed_repos=frozenset({REPO}),
    )


@pytest.fixture
def store() -> InMemoryReviewStore:
    return InMemoryReviewStore()


async def seed_review(store: InMemoryReviewStore) -> None:
    """A prior review, so a mention has findings to talk about."""
    review_id = await store.start_review(
        source="webhook",
        diff_text="d",
        github=GitHubRef(REPO, 1, "a" * 40, "delivery-seed"),
    )
    await store.finish_review(
        review_id,
        ReviewResult(
            findings=[
                MergedFinding(
                    file="app/db.py",
                    line=56,
                    severity="high",
                    category="sql-injection",
                    title="SQL injection in search_tasks",
                    body="`owner` is interpolated into the query.",
                    confidence=0.9,
                    sources=["security"],
                )
            ],
            calls=[
                AgentCall(
                    agent="security", model="claude-haiku-4-5", input_tokens=10, output_tokens=5
                )
            ],
        ),
        elapsed_ms=100,
    )


@pytest.fixture
def build(settings, store, diff_text):
    """A client factory so a test can choose what the model says."""

    def _build(outputs: dict | None = None, self_login: str | None = SELF):
        posted: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            posted.append(request)
            if request.url.path.endswith("/reviews"):
                return httpx.Response(200, json={"id": 999})
            if "/comments" in request.url.path:
                return httpx.Response(201, json={"id": 777})
            return httpx.Response(200, text=diff_text)

        transport, _ = make_transport(handler)
        stub = StubAnthropic(
            responses={"security": [finding()]},
            outputs=outputs or {},
        )
        deps = JobDeps(
            settings=settings,
            graph=build_review_graph(stub, settings),
            github=GitHubClient(token="t", transport=transport),
            store=store,
            client=stub,
            self_login=self_login,
        )
        client = TestClient(create_app(deps))
        return client, posted, stub

    return _build


def post(client, body: bytes, event: str = "issue_comment", **kw):
    kw.setdefault("delivery", str(uuid4()))
    return client.post(
        "/webhooks/github",
        content=body,
        headers=signed_headers(body, SECRET, event=event, **kw),
    )


class TestGuards:
    def test_the_bots_own_comment_is_ignored(self, build, store) -> None:
        """The measured one. Every other guard here passes for the bot's own
        comments — same login, `User` type, `OWNER` association — so this is the
        only thing that stops one review becoming fifteen re-triggers."""
        client, _, stub = build()
        with client:
            response = post(client, comment_payload(login=SELF))

        assert response.status_code == 200
        assert response.json()["status"] == "ignored"
        assert not store.reviews
        assert stub.parse_calls == []

    def test_the_check_is_case_insensitive(self, build, store) -> None:
        client, _, _ = build()
        with client:
            post(client, comment_payload(login="OElborki"))

        assert not store.reviews

    def test_a_bot_account_is_ignored(self, build, store) -> None:
        client, _, _ = build()
        with client:
            post(client, comment_payload(login="dependabot[bot]", user_type="Bot"))

        assert not store.reviews

    @pytest.mark.parametrize("association", ["NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR"])
    def test_an_untrusted_commenter_is_ignored(self, build, store, association) -> None:
        """Anyone who can comment could otherwise spend the budget."""
        client, _, _ = build()
        with client:
            post(client, comment_payload(association=association))

        assert not store.reviews

    def test_a_comment_without_the_handle_is_ignored(self, build, store) -> None:
        client, _, _ = build()
        with client:
            post(client, comment_payload(body="this looks fine to me"))

        assert not store.reviews

    def test_an_edited_comment_is_not_a_request(self, build, store) -> None:
        client, _, _ = build()
        with client:
            post(client, comment_payload(action="edited"))

        assert not store.reviews

    def test_a_comment_on_an_issue_is_ignored(self, build, store) -> None:
        """`issue_comment` fires for issues too, and an issue has no diff."""
        client, _, _ = build()
        with client:
            post(client, comment_payload(is_pr=False))

        assert not store.reviews

    def test_an_unlisted_repository_is_refused(self, build, store) -> None:
        client, _, _ = build()
        body = comment_payload().replace(REPO.encode(), b"someone/else")
        with client:
            response = post(client, body)

        assert response.status_code == 403
        assert not store.reviews

    def test_the_rate_limit_stops_repeated_mentions(self, build, settings, store) -> None:
        """The self-login guard stops the bot; this stops everyone else."""
        client, _, _ = build(
            outputs={"CommentIntent": CommentIntent(action="answer_question", rationale="q")}
        )
        with client:
            for _ in range(settings.max_mention_responses_per_hour):
                post(client, comment_payload())
            extra = post(client, comment_payload())

        assert extra.json()["status"] == "ignored"
        assert len(store.reviews) == settings.max_mention_responses_per_hour


class TestDispatch:
    async def test_a_question_is_answered_in_the_conversation(self, build, store) -> None:
        await seed_review(store)
        client, posted, _ = build(
            outputs={
                "CommentIntent": CommentIntent(
                    action="answer_question", question="why?", rationale="a question"
                ),
                "Answer": Answer(reply="Because `owner` is concatenated."),
            }
        )
        with client:
            response = post(client, comment_payload())

        assert response.status_code == 202
        replies = [r for r in posted if r.url.path.endswith("/issues/1/comments")]
        assert len(replies) == 1
        assert "concatenated" in json.loads(replies[0].content)["body"]

    async def test_a_thread_reply_goes_back_into_its_thread(self, build, store) -> None:
        """Not a new conversation about the same line."""
        await seed_review(store)
        client, posted, _ = build(
            outputs={
                "CommentIntent": CommentIntent(
                    action="answer_question", question="why?", rationale="q"
                ),
                "Answer": Answer(reply="Because it is concatenated."),
            }
        )
        with client:
            post(
                client,
                comment_payload(comment_id=4242, path="app/db.py", line=56),
                event="pull_request_review_comment",
            )

        replies = [r for r in posted if "/comments/4242/replies" in r.url.path]
        assert len(replies) == 1

    async def test_a_rebuttal_is_reconsidered(self, build, store) -> None:
        await seed_review(store)
        client, posted, _ = build(
            outputs={
                "CommentIntent": CommentIntent(
                    action="reconsider", target_ordinal=0, rationale="disputes it"
                ),
                "Verdict": Verdict(stands=False, reply="You're right, withdrawing."),
            }
        )
        with client:
            post(client, comment_payload(body="@reviewhive this is validated upstream"))

        replies = [r for r in posted if r.url.path.endswith("/issues/1/comments")]
        assert "withdrawing" in json.loads(replies[0].content)["body"]

    async def test_a_bare_mention_triggers_a_review(self, build, store) -> None:
        """No classifier call at all, and a review is posted rather than a reply."""
        await seed_review(store)
        client, posted, stub = build()
        with client:
            post(client, comment_payload(body="@reviewhive"))

        assert "CommentIntent" not in stub.parse_calls
        assert [r for r in posted if r.url.path.endswith("/pulls/1/reviews")]

    async def test_a_mention_on_an_unreviewed_pull_request_says_so(self, build, store) -> None:
        """Rather than answering questions about a review that never happened."""
        client, posted, _ = build(
            outputs={
                "CommentIntent": CommentIntent(
                    action="answer_question", question="why?", rationale="q"
                )
            }
        )
        with client:
            post(client, comment_payload())

        replies = [r for r in posted if r.url.path.endswith("/issues/1/comments")]
        assert "haven't reviewed this pull request yet" in json.loads(replies[0].content)["body"]


class TestCost:
    async def test_the_mention_is_recorded_as_its_own_run(self, build, store) -> None:
        await seed_review(store)
        client, _, _ = build(
            outputs={
                "CommentIntent": CommentIntent(
                    action="answer_question", question="why?", rationale="q"
                ),
                "Answer": Answer(reply="Because."),
            }
        )
        with client:
            post(client, comment_payload())

        mentions = [s for s in store.reviews.values() if s.source == "mention"]
        assert len(mentions) == 1
        assert mentions[0].status == "succeeded"
        assert {c.agent for c in mentions[0].result.calls} == {"intent", "answer"}

    async def test_a_re_review_records_the_classifier_alongside_the_agents(
        self, build, store
    ) -> None:
        """One row, one finish, every call on it. Finishing twice would duplicate
        the agent rows and trip the one-call-per-agent constraint."""
        await seed_review(store)
        client, _, _ = build(
            outputs={
                "CommentIntent": CommentIntent(action="full_review", rationale="asks again")
            }
        )
        with client:
            post(client, comment_payload(body="@reviewhive please take another look"))

        mention = next(s for s in store.reviews.values() if s.source == "mention")
        agents = [c.agent for c in mention.result.calls]
        assert "intent" in set(agents)
        assert {"security", "style", "architecture"} <= set(agents)
        assert len(agents) == len(set(agents)), "one call per agent per row"
