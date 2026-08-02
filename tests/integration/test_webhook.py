"""The endpoint, offline.

`importorskip` rather than a marker, following `test_db_models.py`: this needs
the `service` extra but no external state, so it should run whenever it can
rather than being deselected by default. CI installs `.[dev,db,service]`, so the
full set runs somewhere.
"""

from __future__ import annotations

import json

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

SECRET = "911cdf77b9ee3ae9472fde9db672d84c"
REPO = "oelborki/reviewhive-demo"


def payload(
    *,
    action: str = "opened",
    repo: str = REPO,
    draft: bool = False,
    number: int = 1,
    sha: str = "a" * 40,
) -> bytes:
    """A `pull_request` delivery, trimmed to the fields the handler reads."""
    return json.dumps(
        {
            "action": action,
            "number": number,
            "repository": {"full_name": repo},
            "pull_request": {
                "number": number,
                "draft": draft,
                "head": {"sha": sha},
                "title": "Add task search",
            },
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


@pytest.fixture
def client(settings, store, diff_text):
    """The real app around stubs, assembled through `create_app(deps=...)`."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/reviews"):
            return httpx.Response(200, json={"id": 4839508909})
        return httpx.Response(200, text=diff_text)

    transport, _ = make_transport(handler)
    stub = StubAnthropic(responses={"security": [finding()]})
    deps = JobDeps(
        settings=settings,
        graph=build_review_graph(stub, settings),
        github=GitHubClient(token="t", transport=transport),
        store=store,
    )
    with TestClient(create_app(deps)) as test_client:
        yield test_client


def post(client, body: bytes, **header_overrides):
    return client.post(
        "/webhooks/github", content=body, headers=signed_headers(body, SECRET, **header_overrides)
    )


class TestAuthentication:
    def test_an_unsigned_delivery_is_rejected(self, client, store) -> None:
        response = client.post("/webhooks/github", content=payload())

        assert response.status_code == 401
        assert not store.reviews

    def test_a_tampered_body_is_rejected(self, client, store) -> None:
        body = payload()
        headers = signed_headers(body, SECRET)

        response = client.post("/webhooks/github", content=body + b" ", headers=headers)

        assert response.status_code == 401
        assert not store.reviews

    def test_the_wrong_secret_is_rejected(self, client, store) -> None:
        body = payload()

        response = client.post(
            "/webhooks/github", content=body, headers=signed_headers(body, "not-the-secret")
        )

        assert response.status_code == 401
        assert not store.reviews

    def test_authentication_precedes_the_allowlist(self, client) -> None:
        """An unsigned delivery for an unlisted repository must answer 401, not
        403 — otherwise an unauthenticated caller can probe which repositories
        are configured."""
        response = client.post("/webhooks/github", content=payload(repo="someone/else"))

        assert response.status_code == 401


class TestRouting:
    def test_ping_is_answered(self, client, store) -> None:
        """The first thing GitHub sends when a hook is created. A 4xx here shows
        a red cross on the configuration page before anything is wrong."""
        response = post(client, b"{}", event="ping")

        assert response.status_code == 200
        assert not store.reviews

    def test_an_unhandled_event_is_ignored_not_rejected(self, client, store) -> None:
        response = post(client, payload(), event="star")

        assert response.status_code == 200
        assert not store.reviews

    def test_a_signed_but_unparseable_body_is_a_bad_request(self, client) -> None:
        """Reachable precisely because the signature is checked over bytes rather
        than over a parsed payload."""
        response = post(client, b"{not json")

        assert response.status_code == 400

    def test_an_unlisted_repository_is_refused(self, client, store) -> None:
        response = post(client, payload(repo="someone/else"))

        assert response.status_code == 403
        assert not store.reviews

    @pytest.mark.parametrize("action", ["edited", "labeled", "closed", "synchronize"])
    def test_actions_outside_the_trigger_set_are_ignored(self, client, store, action) -> None:
        """A cost control, not tidiness: `pull_request` fires for six actions and
        an unfiltered handler bills for every one."""
        response = post(client, payload(action=action))

        assert response.status_code == 200
        assert not store.reviews

    def test_a_draft_is_ignored(self, client, store) -> None:
        response = post(client, payload(draft=True))

        assert response.status_code == 200
        assert not store.reviews

    def test_ready_for_review_triggers(self, client, store) -> None:
        assert post(client, payload(action="ready_for_review")).status_code == 202


class TestAccepting:
    def test_a_valid_delivery_is_accepted_with_a_review_id(self, client, store) -> None:
        response = post(client, payload())

        assert response.status_code == 202
        assert response.json()["status"] == "accepted"
        assert len(store.reviews) == 1
        assert str(store.only.id) == response.json()["review_id"]

    def test_the_pull_request_is_recorded(self, client, store) -> None:
        post(client, payload(number=7, sha="b" * 40))

        assert store.only.github.repo_full_name == REPO
        assert store.only.github.pr_number == 7
        assert store.only.github.head_sha == "b" * 40

    def test_the_review_runs(self, client, store) -> None:
        """`TestClient` runs background tasks before returning, so by the time the
        response arrives the job has completed. That is convenient here and would
        be misleading in a test asserting the row is still pending."""
        post(client, payload())

        assert store.only.status == "succeeded"
        assert store.only.posted_review_id == 4839508909


class TestIdempotency:
    def test_the_same_delivery_twice_reviews_once(self, client, store) -> None:
        """GitHub redelivers on timeout and offers a Redeliver button."""
        first = post(client, payload(), delivery="delivery-a")
        second = post(client, payload(), delivery="delivery-a")

        assert first.status_code == 202
        assert second.status_code == 200
        assert len(store.reviews) == 1

    def test_a_second_delivery_for_the_same_commit_reviews_once(self, client, store) -> None:
        """A draft marked ready immediately after being opened is two deliveries
        describing the same code."""
        first = post(client, payload(action="opened"), delivery="delivery-a")
        second = post(client, payload(action="ready_for_review"), delivery="delivery-b")

        assert first.status_code == 202
        assert second.status_code == 200
        assert len(store.reviews) == 1

    def test_a_new_commit_is_reviewed_again(self, client, store) -> None:
        post(client, payload(sha="a" * 40), delivery="delivery-a")
        second = post(client, payload(sha="c" * 40), delivery="delivery-b")

        assert second.status_code == 202
        assert len(store.reviews) == 2


class TestStatusEndpoints:
    def test_healthz(self, client) -> None:
        assert client.get("/healthz").json() == {"status": "ok"}

    def test_a_review_can_be_followed_up_by_id(self, client, store) -> None:
        review_id = post(client, payload()).json()["review_id"]

        response = client.get(f"/reviews/{review_id}")

        assert response.status_code == 200
        assert response.json()["status"] == "succeeded"

    def test_an_unknown_review_is_not_found(self, client) -> None:
        response = client.get("/reviews/8ba1a2f0-0000-4000-8000-000000000000")

        assert response.status_code == 404
