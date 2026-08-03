"""Talking to GitHub.

A thin transport: it fetches, it posts, and it classifies status codes into
exceptions. It makes no decisions about what to do with them — degrading a
rejected review rather than failing it is policy, and policy lives in `jobs.py`.

One client per process, and that is correctness rather than tuning. Background
tasks outlive the response that dispatched them, so a client owned by a request
would be closed before the job that needs it runs.
"""

from __future__ import annotations

import httpx

API_VERSION = "2022-11-28"
DIFF_MEDIA_TYPE = "application/vnd.github.v3.diff"
JSON_MEDIA_TYPE = "application/vnd.github+json"

# A bot must not gate merges through branch protection: that is a claim about
# correctness this cannot support. Omitting `event` is worse — it creates a
# *pending* review nobody sees until it is submitted by hand. And GitHub forbids
# APPROVE and REQUEST_CHANGES on your own pull request, so on a demo repo owned
# by the person demoing it, anything else 422s on exactly the path being shown.
REVIEW_EVENT = "COMMENT"


class GitHubError(RuntimeError):
    """A request to GitHub did not succeed."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class DiffTooLarge(GitHubError):
    """GitHub declined to render the diff.

    Documented behaviour above roughly 20k lines or 1MB, not an outage, and every
    large real pull request meets it. It has to degrade visibly rather than crash.
    """


class GitHubRateLimited(GitHubError):
    """Rate limited, with the reset time if GitHub gave one."""

    def __init__(self, message: str, *, retry_after: str | None = None) -> None:
        super().__init__(message, status=403)
        self.retry_after = retry_after


class GitHubUnprocessable(GitHubError):
    """422 — GitHub rejected the request body.

    Carries `errors`, which names the offending comment. It is the only diagnostic
    the API offers for a bad anchor, and swallowing it costs the next debugging
    session.
    """

    def __init__(self, message: str, *, errors: object = None) -> None:
        super().__init__(message, status=422)
        self.errors = errors


class GitHubClient:
    def __init__(
        self,
        *,
        token: str,
        base_url: str = "https://api.github.com",
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout, connect=10.0),
            # `retries` covers connection failures only, which is the right amount
            # of resilience to get for free: a 5xx should fail the review and be
            # recorded, not be retried silently at the transport layer.
            # It is mutually exclusive with an injected transport.
            transport=transport or httpx.AsyncHTTPTransport(retries=2),
            # The diff endpoint can redirect, and a redirect body parses as an
            # empty diff rather than failing.
            follow_redirects=True,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": JSON_MEDIA_TYPE,
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "reviewhive",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def whoami(self) -> str:
        """The login this token acts as.

        Called once at startup and cached. It is what stops the bot answering its
        own comments: a reply that quotes the mention it was triggered by would
        otherwise re-trigger the bot, indefinitely, at cost.
        """
        response = await self._client.get("/user")
        self._raise_for_status(response, "read the authenticated user")
        return response.json()["login"]

    async def fetch_pull_request_diff(self, repo_full_name: str, pr_number: int) -> str:
        response = await self._client.get(
            f"/repos/{repo_full_name}/pulls/{pr_number}",
            headers={"Accept": DIFF_MEDIA_TYPE},
        )

        if response.status_code == 406:
            raise DiffTooLarge(
                f"GitHub declined to render the diff for {repo_full_name}#{pr_number}; "
                f"it is above the size it will serve as a unified diff"
            )
        self._raise_for_status(
            response,
            f"fetch the diff for {repo_full_name}#{pr_number}",
            # Everything else on a pull request works without it, so a 403 here
            # reads as a bug in this method rather than a missing scope.
            forbidden_hint="the .diff media type needs the token's Contents: read "
            "permission, separately from Pull requests",
        )

        # Never `response.text`. GitHub sometimes serves the diff with no charset
        # and httpx will then guess. A mis-decoded diff fails the parser, the
        # router skips the agents, and the result is an empty review that reads
        # exactly like a clean one.
        return response.content.decode("utf-8", errors="replace")

    async def create_review(
        self,
        repo_full_name: str,
        pr_number: int,
        *,
        body: str,
        comments: list[dict[str, object]],
        commit_id: str | None,
    ) -> int:
        payload: dict[str, object] = {
            "body": body,
            "event": REVIEW_EVENT,
            "comments": comments,
        }
        if commit_id:
            payload["commit_id"] = commit_id

        response = await self._client.post(
            f"/repos/{repo_full_name}/pulls/{pr_number}/reviews", json=payload
        )
        self._raise_for_status(response, f"post a review to {repo_full_name}#{pr_number}")
        return response.json()["id"]

    async def create_issue_comment(self, repo_full_name: str, pr_number: int, body: str) -> int:
        """Comment on the pull request's conversation.

        Pull request conversation comments live on the *issues* endpoint, which is
        also why a fine-grained token needs Issues: write to post one.
        """
        response = await self._client.post(
            f"/repos/{repo_full_name}/issues/{pr_number}/comments", json={"body": body}
        )
        self._raise_for_status(response, f"comment on {repo_full_name}#{pr_number}")
        return response.json()["id"]

    async def reply_to_review_comment(
        self, repo_full_name: str, pr_number: int, comment_id: int, body: str
    ) -> int:
        """Reply inside an existing inline comment thread.

        A distinct endpoint from creating a review comment: this one keeps the
        reply in the thread the reviewer is reading, rather than starting a second
        conversation about the same line.
        """
        response = await self._client.post(
            f"/repos/{repo_full_name}/pulls/{pr_number}/comments/{comment_id}/replies",
            json={"body": body},
        )
        self._raise_for_status(
            response, f"reply to comment {comment_id} on {repo_full_name}#{pr_number}"
        )
        return response.json()["id"]

    @staticmethod
    def _raise_for_status(
        response: httpx.Response, action: str, *, forbidden_hint: str | None = None
    ) -> None:
        if response.is_success:
            return

        detail = _message_of(response)

        if response.status_code == 403:
            # Distinguished with one `if` because it is the failure you meet while
            # demoing, and "403" on its own tells you nothing about which it was.
            if response.headers.get("x-ratelimit-remaining") == "0" or response.headers.get(
                "retry-after"
            ):
                raise GitHubRateLimited(
                    f"rate limited trying to {action}: {detail}",
                    retry_after=response.headers.get("retry-after"),
                )
            hint = f" ({forbidden_hint})" if forbidden_hint else ""
            raise GitHubError(f"not permitted to {action}{hint}: {detail}", status=403)

        if response.status_code == 422:
            body = _json_or_none(response) or {}
            raise GitHubUnprocessable(
                f"GitHub rejected the request to {action}: {detail}",
                errors=body.get("errors") if isinstance(body, dict) else None,
            )

        raise GitHubError(
            f"failed to {action}: HTTP {response.status_code} {detail}",
            status=response.status_code,
        )


def _json_or_none(response: httpx.Response) -> object | None:
    try:
        return response.json()
    except ValueError:
        return None


def _message_of(response: httpx.Response) -> str:
    body = _json_or_none(response)
    if isinstance(body, dict) and isinstance(body.get("message"), str):
        return body["message"]
    return response.text[:200]
