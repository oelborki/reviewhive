"""The endpoint GitHub delivers to.

The ordering below is load-bearing and each step earns its place:

1. the raw body is read before anything parses it, because the signature covers
   the bytes GitHub sent and a JSON round trip changes them;
2. the signature is checked before the allowlist, so an unauthenticated caller
   cannot learn which repositories are configured;
3. deliberate non-action answers 200 rather than 4xx, because a red delivery in
   GitHub's log should mean something is wrong.
"""

from __future__ import annotations

import json
import logging
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Request, Response
from fastapi.responses import JSONResponse

from reviewhive.github.signature import HEADER, verify
from reviewhive.jobs import PullRequestRef, review_pull_request
from reviewhive.persistence import DuplicateDelivery, GitHubRef

logger = logging.getLogger(__name__)

router = APIRouter()

# Everything else `pull_request` fires for — edited, labeled, assigned, closed,
# review_requested — is ignored. That filter is a cost control, not tidiness: an
# unfiltered handler reviews the same pull request six times and bills for each.
TRIGGER_ACTIONS = frozenset({"opened", "reopened", "ready_for_review"})

_IGNORED = JSONResponse({"status": "ignored"}, status_code=200)


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/reviews/{review_id}")
async def get_review(review_id: UUID, request: Request) -> Response:
    """What became of a review the webhook accepted.

    The 202 hands back an id and nothing else, so without this the only way to
    see whether the background job finished is SQL.
    """
    from reviewhive.api.app import get_deps

    found = await get_deps(request).store.get_review(review_id)
    if found is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"review_id": str(found.id), "status": found.status})


@router.post("/webhooks/github")
async def github_webhook(request: Request, background_tasks: BackgroundTasks) -> Response:
    from reviewhive.api.app import get_deps

    deps = get_deps(request)
    settings = deps.settings

    # 1. Raw bytes, before anything can parse and re-serialise them.
    body = await request.body()

    # 2. Authenticate. Every failure answers the same way; which check failed is
    #    a fact about our configuration.
    if not verify(body, request.headers.get(HEADER), settings.github_webhook_secret):
        logger.warning("rejected a delivery with an invalid signature")
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    event = request.headers.get("X-GitHub-Event", "")
    if event == "ping":
        # The first thing GitHub sends when a hook is created. Answering 4xx puts
        # a red cross on the configuration page before anything is wrong.
        return JSONResponse({"status": "pong"})
    if event != "pull_request":
        return _IGNORED

    # 3. Only now is it safe to look inside.
    try:
        payload = json.loads(body)
    except ValueError:
        return JSONResponse({"error": "malformed payload"}, status_code=400)

    repo = (payload.get("repository") or {}).get("full_name", "")
    if repo.lower() not in settings.allowed_repos:
        logger.warning("rejected a delivery for unlisted repository %r", repo)
        return JSONResponse({"error": "repository not allowed"}, status_code=403)

    action = payload.get("action", "")
    triggers = TRIGGER_ACTIONS | ({"synchronize"} if settings.review_on_synchronize else set())
    if action not in triggers:
        return _IGNORED

    pull_request = payload.get("pull_request") or {}
    if pull_request.get("draft"):
        # A draft is explicitly not ready to be read. It becomes reviewable via
        # ready_for_review, which is in the trigger set.
        return _IGNORED

    ref = PullRequestRef(
        repo_full_name=repo,
        pr_number=payload.get("number") or pull_request.get("number"),
        head_sha=(pull_request.get("head") or {}).get("sha", ""),
    )
    delivery_id = request.headers.get("X-GitHub-Delivery", "")

    # 4. Idempotency. The lookups are the fast path; the unique constraint behind
    #    start_review is the guarantee, because two concurrent redeliveries both
    #    pass these checks.
    if delivery_id and await deps.store.find_review_by_delivery(delivery_id):
        logger.info("delivery %s already recorded", delivery_id)
        return _IGNORED

    already = await deps.store.find_latest_review_for_head(
        repo_full_name=ref.repo_full_name, pr_number=ref.pr_number, head_sha=ref.head_sha
    )
    if already is not None and already.status != "failed":
        # A draft marked ready right after being opened is the real case. Only a
        # failed review is worth repeating.
        logger.info("%s#%s at %s already reviewed", repo, ref.pr_number, ref.head_sha[:7])
        return _IGNORED

    # 5. Persist before dispatching, both inside the request. The write is one
    #    local round trip, well inside GitHub's delivery timeout, and it is what
    #    lets a crashed job leave evidence.
    try:
        review_id = await deps.store.start_review(
            source="webhook",
            github=GitHubRef(
                repo_full_name=ref.repo_full_name,
                pr_number=ref.pr_number,
                head_sha=ref.head_sha,
                delivery_id=delivery_id,
            ),
        )
    except DuplicateDelivery:
        # Lost the race against a concurrent redelivery. The other one is doing
        # the work.
        return _IGNORED

    # Background tasks run after the response is sent, so this costs nothing now.
    background_tasks.add_task(review_pull_request, deps, ref, review_id)

    return JSONResponse(
        {"review_id": str(review_id), "status": "accepted"}, status_code=202
    )
