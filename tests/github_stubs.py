"""Offline stand-ins for GitHub.

The same shape as `stubs.py` (Anthropic) and `fakes.py` (storage): a hand-built
double injected at construction, so tests exercise the real code path with no
monkeypatching. `httpx.MockTransport` rather than a recording library — the
client already takes a transport for exactly this, and a new dependency would
buy nothing the constructor does not already give.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from reviewhive.github.signature import sign


def make_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """A transport plus the log of what was sent through it.

    The log is what lets a test assert on headers and bodies the client built,
    which is most of what there is to get wrong here.
    """
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.MockTransport(record), seen


def always(response: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    """Answer every request the same way."""
    return lambda _request: response


def signed_headers(
    body: bytes,
    secret: str,
    *,
    event: str = "pull_request",
    delivery: str = "00000000-0000-0000-0000-000000000000",
) -> dict[str, str]:
    """Headers GitHub would send for `body`.

    Signs the exact bytes the test will post. A helper that took a dict and
    serialised it here would reintroduce the round trip the verifier exists to
    catch.
    """
    return {
        "X-Hub-Signature-256": sign(body, secret),
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "Content-Type": "application/json",
        "User-Agent": "GitHub-Hookshot/stub",
    }
