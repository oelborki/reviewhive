"""The HTTP client, against a stub transport.

No `importorskip`: httpx arrives with `anthropic`, so this runs on a bare
install alongside the rest of the offline suite.
"""

from __future__ import annotations

import json

import httpx
import pytest
from tests.github_stubs import always, make_transport

from reviewhive.github.client import (
    DiffTooLarge,
    GitHubClient,
    GitHubError,
    GitHubRateLimited,
    GitHubUnprocessable,
)

REPO = "oelborki/reviewhive-demo"
DIFF = (
    "diff --git a/app/db.py b/app/db.py\n"
    "--- a/app/db.py\n"
    "+++ b/app/db.py\n"
    "@@ -1,2 +1,3 @@\n"
    " import sqlite3\n"
    "+DB_PATH = 'tasks.db'\n"
    " \n"
)


def client(handler) -> tuple[GitHubClient, list[httpx.Request]]:
    transport, seen = make_transport(handler)
    return GitHubClient(token="github_pat_test", transport=transport), seen


class TestFetchDiff:
    async def test_asks_for_the_diff_media_type_with_bearer_auth(self) -> None:
        gh, seen = client(always(httpx.Response(200, text=DIFF)))

        await gh.fetch_pull_request_diff(REPO, 1)

        assert seen[0].url.path == f"/repos/{REPO}/pulls/1"
        assert seen[0].headers["accept"] == "application/vnd.github.v3.diff"
        assert seen[0].headers["authorization"] == "Bearer github_pat_test"
        assert seen[0].headers["x-github-api-version"] == "2022-11-28"

    async def test_returns_the_diff_text(self) -> None:
        gh, _ = client(always(httpx.Response(200, text=DIFF)))

        assert await gh.fetch_pull_request_diff(REPO, 1) == DIFF

    async def test_decodes_as_utf8_when_github_sends_no_charset(self) -> None:
        """`response.text` would let httpx guess here. A mis-decoded diff fails the
        parser, the router then skips the agents, and the result is an empty review
        indistinguishable from a clean one."""
        body = "diff --git a/x.py b/x.py\n+name = 'café — テスト'\n".encode()
        gh, _ = client(
            always(httpx.Response(200, content=body, headers={"content-type": "text/plain"}))
        )

        assert "café — テスト" in await gh.fetch_pull_request_diff(REPO, 1)

    async def test_406_is_a_size_limit_not_a_crash(self) -> None:
        """Documented behaviour above ~20k lines, met by every large real PR."""
        gh, _ = client(always(httpx.Response(406, json={"message": "too big"})))

        with pytest.raises(DiffTooLarge, match="above the size"):
            await gh.fetch_pull_request_diff(REPO, 1)

    async def test_403_names_the_permission_that_is_usually_missing(self) -> None:
        """Measured: a token with Pull requests but not Contents posts reviews and
        lists files happily, and 403s only here — so the error has to say so or it
        reads as a bug in this method."""
        gh, _ = client(
            always(httpx.Response(403, json={"message": "Resource not accessible"}))
        )

        with pytest.raises(GitHubError, match="Contents: read"):
            await gh.fetch_pull_request_diff(REPO, 1)

    async def test_rate_limiting_is_distinguished_from_a_permission_problem(self) -> None:
        gh, _ = client(
            always(
                httpx.Response(
                    403,
                    json={"message": "API rate limit exceeded"},
                    headers={"x-ratelimit-remaining": "0"},
                )
            )
        )

        with pytest.raises(GitHubRateLimited):
            await gh.fetch_pull_request_diff(REPO, 1)

    async def test_404_names_the_repository(self) -> None:
        """A repo typo and an under-permissioned token are the two commonest
        dev-loop failures; the message has to say which repo it tried."""
        gh, _ = client(always(httpx.Response(404, json={"message": "Not Found"})))

        with pytest.raises(GitHubError, match=REPO):
            await gh.fetch_pull_request_diff(REPO, 1)


class TestCreateReview:
    async def test_posts_a_comment_event_to_the_reviews_endpoint(self) -> None:
        gh, seen = client(always(httpx.Response(200, json={"id": 4839508909})))

        review_id = await gh.create_review(
            REPO, 1, body="summary", comments=[], commit_id="abc123"
        )

        assert review_id == 4839508909
        assert seen[0].method == "POST"
        assert seen[0].url.path == f"/repos/{REPO}/pulls/1/reviews"

    async def test_the_event_is_comment(self) -> None:
        """A bot must not gate merges, and GitHub forbids APPROVE on your own pull
        request — which is precisely the demo path."""
        gh, seen = client(always(httpx.Response(200, json={"id": 1})))

        await gh.create_review(REPO, 1, body="s", comments=[], commit_id=None)

        assert json.loads(seen[0].content)["event"] == "COMMENT"

    async def test_commit_id_is_sent_when_given_and_omitted_when_not(self) -> None:
        gh, seen = client(always(httpx.Response(200, json={"id": 1})))

        await gh.create_review(REPO, 1, body="s", comments=[], commit_id="deadbeef")
        await gh.create_review(REPO, 1, body="s", comments=[], commit_id=None)

        assert json.loads(seen[0].content)["commit_id"] == "deadbeef"
        assert "commit_id" not in json.loads(seen[1].content)

    async def test_422_carries_githubs_own_diagnostic(self) -> None:
        """The errors array names the offending comment. It is the only clue the
        API gives about which anchor it disliked."""
        gh, _ = client(
            always(
                httpx.Response(
                    422,
                    json={
                        "message": "Validation Failed",
                        "errors": [{"resource": "PullRequestReviewComment", "field": "line"}],
                    },
                )
            )
        )

        with pytest.raises(GitHubUnprocessable) as caught:
            await gh.create_review(REPO, 1, body="s", comments=[{}], commit_id="x")

        assert "Validation Failed" in str(caught.value)
        assert caught.value.errors == [
            {"resource": "PullRequestReviewComment", "field": "line"}
        ]


class TestWhoami:
    async def test_returns_the_login(self) -> None:
        gh, seen = client(always(httpx.Response(200, json={"login": "oelborki"})))

        assert await gh.whoami() == "oelborki"
        assert seen[0].url.path == "/user"
