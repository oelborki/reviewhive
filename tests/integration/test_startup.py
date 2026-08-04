"""Refusing to start on a configuration that would be unsafe to run.

These guard the failure mode that looks healthy. A service with no webhook
secret accepts every delivery and reports nothing wrong; a service with an empty
allowlist rejects every delivery and also reports nothing wrong. Both are caught
once at startup rather than per request, so this is the only place that can test
them.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", exc_type=ImportError, reason="requires the service extra")

import httpx
from fastapi.testclient import TestClient
from tests.fakes import InMemoryReviewStore
from tests.github_stubs import always, make_transport
from tests.stubs import StubAnthropic

from reviewhive.api.app import MisconfiguredService, _require, create_app
from reviewhive.config import Settings
from reviewhive.github.client import GitHubClient
from reviewhive.graph.build import build_review_graph
from reviewhive.jobs import JobDeps

REPO = "oelborki/reviewhive-demo"


def settings(**overrides) -> Settings:
    base = {
        "anthropic_api_key": "test",
        "github_token": "github_pat_test",
        "github_webhook_secret": "s3cret",
        "allowed_repos": frozenset({REPO}),
    }
    return Settings(**{**base, **overrides})


class TestRequire:
    def test_a_complete_configuration_is_accepted(self) -> None:
        _require(settings())

    @pytest.mark.parametrize("blank", [None, ""])
    def test_no_webhook_secret_refuses_to_start(self, blank) -> None:
        """The dangerous one. Without a secret the endpoint verifies nothing,
        accepts anything, and looks perfectly healthy — and everything it accepts
        spends money."""
        with pytest.raises(MisconfiguredService, match="WEBHOOK_SECRET"):
            _require(settings(github_webhook_secret=blank))

    @pytest.mark.parametrize("blank", [None, ""])
    def test_no_token_refuses_to_start(self, blank) -> None:
        """Without it the diff fetch fails on every delivery, after the row has
        been written and the work dispatched."""
        with pytest.raises(MisconfiguredService, match="GITHUB_TOKEN"):
            _require(settings(github_token=blank))

    def test_an_empty_allowlist_refuses_to_start(self) -> None:
        """Deny-by-default is right for the allowlist and wrong as a silent
        default: every delivery would 403 and the service would look fine, which
        is a forgotten setting rather than a decision."""
        with pytest.raises(MisconfiguredService, match="ALLOWED_REPOS"):
            _require(settings(allowed_repos=frozenset()))

    def test_every_missing_credential_is_named_at_once(self) -> None:
        """One restart per missing variable is a bad way to learn a config."""
        with pytest.raises(MisconfiguredService) as caught:
            _require(settings(github_webhook_secret=None, github_token=None))

        assert "REVIEWHIVE_GITHUB_WEBHOOK_SECRET" in str(caught.value)
        assert "REVIEWHIVE_GITHUB_TOKEN" in str(caught.value)


class TestLifespan:
    def test_an_unconfigured_service_fails_to_boot(self) -> None:
        """The check that matters. The tests above prove `_require` rejects a bad
        configuration; this proves it is actually on the startup path, which is
        the part a refactor can quietly remove.

        The conftest fixture scrubs every REVIEWHIVE_* variable and disables the
        .env file, so a real `create_app()` here sees no credentials at all.
        """
        with pytest.raises(MisconfiguredService), TestClient(create_app()):
            pass  # pragma: no cover — lifespan raises before the body runs

    def test_injected_dependencies_skip_the_configuration_check(self) -> None:
        """`create_app(deps=...)` owns nothing, so it validates nothing. This is
        what lets the webhook tests run against stub credentials — and it is worth
        pinning, because making the check unconditional would break them in a way
        that looks like a test problem rather than a design one."""
        transport, _ = make_transport(always(httpx.Response(200, json={})))
        stub_settings = settings(github_webhook_secret=None, github_token=None)
        deps = JobDeps(
            settings=stub_settings,
            graph=build_review_graph(StubAnthropic(), stub_settings),
            github=GitHubClient(token="t", transport=transport),
            store=InMemoryReviewStore(),
        )

        with TestClient(create_app(deps)) as client:
            assert client.get("/healthz").status_code == 200
