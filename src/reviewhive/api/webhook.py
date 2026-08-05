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
from reviewhive.mentions.dispatch import TRUSTED_ASSOCIATIONS, respond_to_mention
from reviewhive.persistence import DuplicateDelivery, GitHubRef

logger = logging.getLogger(__name__)

router = APIRouter()

# Everything else `pull_request` fires for — edited, labeled, assigned, closed,
# review_requested — is ignored. That filter is a cost control, not tidiness: an
# unfiltered handler reviews the same pull request six times and bills for each.
TRIGGER_ACTIONS = frozenset({"opened", "reopened", "ready_for_review"})

# Comment events the bot listens to. `issue_comment` carries pull request
# conversation comments; `pull_request_review_comment` carries replies inside an
# inline thread, and is the only one that says which comment is being replied to.
COMMENT_EVENTS = frozenset({"issue_comment", "pull_request_review_comment"})

_IGNORED = JSONResponse({"status": "ignored"}, status_code=200)


def _ignored(reason: str, *args: object, level: int = logging.INFO) -> JSONResponse:
    """A 200 that did nothing, plus the reason it did nothing.

    The response body is identical for every exit on purpose: which check
    stopped a delivery is a fact about our configuration, and a caller learns
    nothing from it. The reason belongs in the log — and it has to be attached
    at *every* exit rather than at the interesting ones. Eight of these returns
    used to be silent, which made them indistinguishable from the ones that
    explained themselves, and "the webhook answered 200 and did nothing" was
    already the most common thing anyone had to debug.

    Taking the reason as a required argument is the point of the helper. A new
    early return cannot be added without saying why.
    """
    logger.log(level, "ignored: " + reason, *args)
    return _IGNORED


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


async def _handle_comment(
    request: Request,
    background_tasks: BackgroundTasks,
    deps,
    body: bytes,
    event: str,
) -> Response:
    """A comment that might be talking to the bot.

    The guard order here is not arbitrary and is not the one the plan assumed.
    Measured against a real review: posting fifteen inline comments fired fifteen
    `pull_request_review_comment` deliveries, every one of them with
    `sender.type == "User"` and `author_association == "OWNER"` — because the bot
    acts as a personal access token belonging to a person. So the bot-sender check
    and the association check are both useless against the bot itself, and the
    self-login comparison is the only thing standing between one review and fifteen
    re-triggers, each able to cause fifteen more. It goes first.
    """
    settings = deps.settings

    try:
        payload = json.loads(body)
    except ValueError:
        return JSONResponse({"error": "malformed payload"}, status_code=400)

    repo = (payload.get("repository") or {}).get("full_name", "")
    if repo.lower() not in settings.allowed_repos:
        return JSONResponse({"error": "repository not allowed"}, status_code=403)

    # Edits and deletions are not requests.
    if payload.get("action") != "created":
        return _ignored("comment action is %r, not 'created'", payload.get("action"))

    comment = payload.get("comment") or {}
    sender = (comment.get("user") or payload.get("sender") or {})
    login = sender.get("login", "")

    if deps.self_login and login.lower() == deps.self_login.lower():
        # The loop guard, and the only one that works. Everything below would
        # happily let the bot answer itself.
        # Debug, not info: one posted review fires fifteen of these.
        return _ignored("the comment is our own", level=logging.DEBUG)

    if sender.get("type") == "Bot" or login.endswith("[bot]"):
        return _ignored("sender %r is a bot", login)

    if comment.get("author_association") not in TRUSTED_ASSOCIATIONS:
        # Silently. Explaining the refusal to a stranger tells them the bot is
        # here and costs a comment doing it.
        return _ignored(
            "%r is %s, which is not a trusted association",
            login,
            comment.get("author_association"),
        )

    text = comment.get("body") or ""
    if settings.mention_handle.lower() not in text.lower():
        # Debug for the same reason as the self-comment guard: this is every
        # ordinary comment in the repository, and at info it would drown the rest.
        return _ignored("no %s in the comment", settings.mention_handle, level=logging.DEBUG)

    # `issue_comment` fires for issues as well as pull requests.
    issue = payload.get("issue") or {}
    if event == "issue_comment" and not issue.get("pull_request"):
        return _ignored("the comment is on an issue, not a pull request")

    pr_number = issue.get("number") or (payload.get("pull_request") or {}).get("number")
    if pr_number is None:
        return _ignored("no pull request number in the payload")

    recent = await deps.store.count_recent_mentions(
        repo_full_name=repo, pr_number=pr_number, within_seconds=3600
    )
    if recent >= settings.max_mention_responses_per_hour:
        return _ignored(
            "mention rate limit of %d/hour reached on %s#%s",
            settings.max_mention_responses_per_hour,
            repo,
            pr_number,
            level=logging.WARNING,
        )

    head_sha = (payload.get("pull_request") or {}).get("head", {}).get("sha", "")
    ref = PullRequestRef(repo_full_name=repo, pr_number=pr_number, head_sha=head_sha)

    try:
        review_id = await deps.store.start_review(
            source="mention",
            github=GitHubRef(
                repo_full_name=repo,
                pr_number=pr_number,
                head_sha=head_sha,
                delivery_id=request.headers.get("X-GitHub-Delivery", ""),
            ),
        )
    except DuplicateDelivery:
        # Comment deliveries are redelivered on timeout exactly like pull request
        # ones, and answering the same comment twice is both confusing and paid for.
        return _ignored("this comment delivery is already recorded")

    background_tasks.add_task(
        respond_to_mention,
        deps,
        ref,
        review_id,
        comment=text,
        # Only a review-comment reply knows which thread it is in; a conversation
        # comment does not, and the classifier is asked to guess there instead.
        in_reply_to=comment.get("id") if event == "pull_request_review_comment" else None,
        comment_path=comment.get("path"),
        comment_line=comment.get("line") or comment.get("original_line"),
    )

    return JSONResponse({"review_id": str(review_id), "status": "accepted"}, status_code=202)


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
    if event in COMMENT_EVENTS:
        return await _handle_comment(request, background_tasks, deps, body, event)
    if event != "pull_request":
        return _ignored("event %r is not one this service reviews", event)

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
        # `synchronize` lands here unless review_on_synchronize is on, and that is
        # the one people expect to trigger and are surprised by.
        return _ignored("action %r is not a trigger; triggers are %s", action, sorted(triggers))

    pull_request = payload.get("pull_request") or {}
    if pull_request.get("draft"):
        # A draft is explicitly not ready to be read. It becomes reviewable via
        # ready_for_review, which is in the trigger set.
        return _ignored("%s#%s is a draft", repo, payload.get("number"))

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
        return _ignored("delivery %s is already recorded", delivery_id)

    already = await deps.store.find_latest_review_for_head(
        repo_full_name=ref.repo_full_name, pr_number=ref.pr_number, head_sha=ref.head_sha
    )
    if already is not None and already.status != "failed":
        # A draft marked ready right after being opened is the real case. Only a
        # failed review is worth repeating.
        return _ignored(
            "%s#%s at %s is already reviewed (%s)",
            repo,
            ref.pr_number,
            ref.head_sha[:7],
            already.status,
        )

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
        return _ignored("lost the race with a concurrent redelivery of %s", delivery_id)

    # Background tasks run after the response is sent, so this costs nothing now.
    background_tasks.add_task(review_pull_request, deps, ref, review_id)

    return JSONResponse(
        {"review_id": str(review_id), "status": "accepted"}, status_code=202
    )
