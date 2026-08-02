"""The check a reviewer looks for first.

No `importorskip`: this is stdlib only, so it runs on a bare install.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from reviewhive.github.signature import HEADER, MissingSecret, sign, verify

SECRET = "911cdf77b9ee3ae9472fde9db672d84c"
BODY = b'{"action":"opened","number":1}'


def test_a_signature_we_produced_verifies() -> None:
    assert verify(BODY, sign(BODY, SECRET), SECRET)


def test_the_header_matches_githubs_documented_shape() -> None:
    """Built from the spec rather than from our own `sign`, so the two cannot
    agree on something GitHub does not send."""
    expected = "sha256=" + hmac.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()
    assert sign(BODY, SECRET) == expected
    assert HEADER == "X-Hub-Signature-256"


def test_a_payload_with_non_ascii_verifies() -> None:
    """The case that proves we sign bytes rather than re-serialised JSON. A
    handler that parsed the body and dumped it again would escape these
    differently and fail here while passing every ASCII test."""
    body = '{"title":"café — テスト \U0001f600"}'.encode()
    assert verify(body, sign(body, SECRET), SECRET)


def test_an_empty_body_verifies() -> None:
    assert verify(b"", sign(b"", SECRET), SECRET)


def test_one_changed_byte_is_rejected() -> None:
    assert not verify(BODY + b" ", sign(BODY, SECRET), SECRET)


def test_a_different_secret_is_rejected() -> None:
    assert not verify(BODY, sign(BODY, "another-secret"), SECRET)


def test_a_truncated_digest_is_rejected() -> None:
    """Guards a `startswith` or slice comparison. A prefix of the real digest is
    the exact input that a naive implementation accepts."""
    valid = sign(BODY, SECRET)
    assert not verify(BODY, valid[:-8], SECRET)


def test_a_missing_header_is_rejected() -> None:
    assert not verify(BODY, None, SECRET)


def test_a_bare_digest_without_the_prefix_is_rejected() -> None:
    assert not verify(BODY, sign(BODY, SECRET).removeprefix("sha256="), SECRET)


def test_a_sha1_header_is_rejected() -> None:
    """GitHub still sends `X-Hub-Signature` alongside the sha256 header. Accepting
    it would let a caller choose the broken algorithm."""
    digest = hmac.new(SECRET.encode(), BODY, hashlib.sha1).hexdigest()
    assert not verify(BODY, f"sha1={digest}", SECRET)


@pytest.mark.parametrize("digest", ["sha256=zzzz", "sha256=", "sha256=nothex!!"])
def test_a_malformed_digest_is_rejected_rather_than_raising(digest: str) -> None:
    """`bytes.fromhex` on this input raises ValueError, which would turn a bad
    signature into a 500. It has to come back as a plain no."""
    assert not verify(BODY, digest, SECRET)


@pytest.mark.parametrize("secret", [None, ""])
def test_an_unset_secret_raises_rather_than_accepting(secret: str | None) -> None:
    """The failure mode this prevents is a deployment that verifies nothing and
    looks fine. It has to be loud, and it is checked at startup."""
    with pytest.raises(MissingSecret):
        verify(BODY, sign(BODY, SECRET), secret)
    with pytest.raises(MissingSecret):
        sign(BODY, secret or "")
