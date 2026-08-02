"""Proving a delivery came from GitHub.

The signature covers the **exact bytes** GitHub sent. Nothing here accepts a
parsed payload, and the handler must read the raw body before anything touches
it: re-serialising JSON reorders keys, changes whitespace and escapes unicode
differently, and the resulting mismatch reads as a broken HMAC rather than as
the round trip it is.

Every rejection returns the same `False`. Which check failed is a fact about our
configuration, and an unauthenticated caller has no business learning it.
"""

from __future__ import annotations

import hashlib
import hmac

# GitHub sends both when a hook is old enough; sha1 is broken and is not accepted
# here, so a downgrade to it fails closed rather than quietly verifying.
HEADER = "X-Hub-Signature-256"
_PREFIX = "sha256="


class MissingSecret(RuntimeError):
    """No webhook secret is configured.

    Raised rather than returning False so that an unconfigured deployment cannot
    look like a stream of bad signatures. A `None` secret that accepted anything
    would be a public endpoint that spends money on request, so this is checked
    at startup and never at the first delivery.
    """


def sign(body: bytes, secret: str) -> str:
    """The header value GitHub would send for this body.

    Exposed so `scripts/replay_webhook.py` signs with the same code the verifier
    uses. A second implementation in the script would be free to drift, and the
    drift would present as a signature bug in the server.
    """
    if not secret:
        raise MissingSecret("a webhook secret is required to sign a delivery")
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return _PREFIX + digest


def verify(body: bytes, header: str | None, secret: str | None) -> bool:
    """Whether `header` is a valid signature over `body`."""
    if not secret:
        raise MissingSecret("a webhook secret is required to verify a delivery")
    if not header or not header.startswith(_PREFIX):
        return False

    # compare_digest over the full header rather than the decoded digest: it is
    # already a fixed-length ASCII string, so there is nothing to gain by
    # unhexing, and bytes.fromhex on a malformed digest raises ValueError --
    # which would turn a bad signature into a 500 instead of a 401.
    return hmac.compare_digest(header, sign(body, secret))
