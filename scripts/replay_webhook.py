"""Deliver a captured webhook payload to a local server.

The inner loop for webhook work. Opening a real pull request per iteration is
slow, costs a review, and cannot be repeated; this replays a saved delivery as
many times as you like.

It signs the exact bytes it sends, using the same `sign()` the server verifies
with. That matters twice over: a saved fixture can never carry a reusable
signature, because storing it re-serialises the JSON and changes the bytes — the
very round trip the verifier exists to catch — and a second signing
implementation here would be free to drift from the one under test.

    python scripts/replay_webhook.py tests/fixtures/webhooks/pull_request_opened.json
    python scripts/replay_webhook.py <fixture> --repo you/demo --pr 7 --sha $(git rev-parse HEAD)
    python scripts/replay_webhook.py <fixture> --delivery <id>   # replay one, watch dedupe
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

import httpx

from reviewhive.config import get_settings
from reviewhive.github.signature import HEADER, MissingSecret, sign


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("payload", type=Path, help="a captured payload, e.g. from a fixture")
    parser.add_argument("--url", default="http://127.0.0.1:8000/webhooks/github")
    parser.add_argument("--event", default=None, help="X-GitHub-Event; inferred from the file")
    parser.add_argument(
        "--delivery",
        default=None,
        help="X-GitHub-Delivery; a fresh id each run unless given. Pass a previous "
        "one to replay it and watch the idempotency check fire.",
    )
    parser.add_argument("--repo", default=None, help="rewrite repository.full_name")
    parser.add_argument("--pr", type=int, default=None, help="rewrite the pull request number")
    parser.add_argument("--sha", default=None, help="rewrite the head sha")
    parser.add_argument("--action", default=None, help="rewrite the action")
    return parser.parse_args()


def retarget(payload: dict, args: argparse.Namespace) -> dict:
    """Aim a captured payload at a different pull request."""
    if args.repo:
        payload.setdefault("repository", {})["full_name"] = args.repo
        owner, _, name = args.repo.partition("/")
        payload["repository"]["name"] = name
        payload["repository"].setdefault("owner", {})["login"] = owner
    if args.pr is not None:
        payload["number"] = args.pr
        if "pull_request" in payload:
            payload["pull_request"]["number"] = args.pr
    if args.sha and "pull_request" in payload:
        payload["pull_request"].setdefault("head", {})["sha"] = args.sha
    if args.action:
        payload["action"] = args.action
    return payload


def main() -> None:
    args = parse_args()
    if not args.payload.is_file():
        sys.exit(f"no such payload: {args.payload}")

    settings = get_settings()
    try:
        # Re-serialised here, then signed. Sign last, always: anything that
        # rewrites the body after signing invalidates it.
        body = json.dumps(
            retarget(json.loads(args.payload.read_text(encoding="utf-8")), args)
        ).encode("utf-8")
        signature = sign(body, settings.github_webhook_secret or "")
    except MissingSecret:
        sys.exit(
            "REVIEWHIVE_GITHUB_WEBHOOK_SECRET is not set. It must match the secret "
            "configured on the repository's webhook."
        )

    event = args.event or ("ping" if "zen" in json.loads(body) else "pull_request")
    delivery = args.delivery or str(uuid4())

    response = httpx.post(
        args.url,
        content=body,
        headers={
            HEADER: signature,
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "Content-Type": "application/json",
            "User-Agent": "GitHub-Hookshot/replay",
        },
        timeout=30.0,
    )

    print(f"{event} delivery {delivery}")
    print(f"-> {response.status_code} {response.text}")


if __name__ == "__main__":
    main()
