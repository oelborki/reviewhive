from __future__ import annotations

import pytest
from pydantic import ValidationError

from reviewhive.config import Settings


def test_the_suite_is_isolated_from_a_populated_dotenv() -> None:
    """Guards the isolation itself, not a feature.

    `Settings` reads a `.env` file as well as the environment, and the README
    tells developers to create one. Scrubbing environment variables alone leaves
    the file in play, so a filled-in `.env` silently changes what the suite is
    testing — which is how a real database URL first reached these tests. The
    autouse fixture in conftest disables `env_file`; this asserts it worked.
    """
    assert Settings.model_config["env_file"] is None


class TestAllowedRepos:
    """The allowlist is the only thing standing between a public smee channel and
    the Anthropic budget, so its parsing is worth pinning."""

    def test_a_comma_separated_environment_value_is_split(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Must go through the environment. Passing `allowed_repos="a/b,c/d"` as a
        keyword skips pydantic-settings' decoding entirely, so a kwarg version of
        this test passes even when the real path raises SettingsError."""
        monkeypatch.setenv("REVIEWHIVE_ALLOWED_REPOS", "owner/one,owner/two")
        assert Settings().allowed_repos == {"owner/one", "owner/two"}

    def test_entries_are_trimmed_lowercased_and_deduplicated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GitHub sends `full_name` in the repository's own casing; the comparison
        has to survive someone typing it differently."""
        monkeypatch.setenv("REVIEWHIVE_ALLOWED_REPOS", " Owner/Repo , owner/repo ,, ")
        assert Settings().allowed_repos == {"owner/repo"}

    def test_unset_denies_everything(self) -> None:
        """Deny by default. An empty allowlist that allowed everything would make a
        forgotten setting indistinguishable from an open endpoint."""
        assert Settings().allowed_repos == frozenset()

    @pytest.mark.parametrize("value", ["notarepo", "owner/repo/extra", "owner /repo", "/repo"])
    def test_a_malformed_entry_is_rejected_at_load(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Caught at startup rather than at first delivery, where it would present
        as every webhook 403ing for no visible reason."""
        monkeypatch.setenv("REVIEWHIVE_ALLOWED_REPOS", value)
        with pytest.raises(ValidationError, match="owner/repo"):
            Settings()


class TestGitHubCredentials:
    def test_a_bare_github_token_in_the_environment_is_not_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The setting is `REVIEWHIVE_GITHUB_TOKEN` on purpose. GitHub Actions
        injects `GITHUB_TOKEN` into every job, so an alias would hand CI's own
        credential to a suite whose entire premise is that it has none — and the
        conftest scrub only covers the `REVIEWHIVE_` prefix."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghs-injected-by-actions")
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "injected")
        settings = Settings()
        assert settings.github_token is None
        assert settings.github_webhook_secret is None

    def test_the_prefixed_names_are_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REVIEWHIVE_GITHUB_TOKEN", "github_pat_example")
        monkeypatch.setenv("REVIEWHIVE_GITHUB_WEBHOOK_SECRET", "s3cret")
        settings = Settings()
        assert settings.github_token == "github_pat_example"
        assert settings.github_webhook_secret == "s3cret"


class TestDatabaseUrl:
    def test_unset_is_valid_and_means_do_not_persist(self) -> None:
        """The CLI has to run with no database. An absent URL is a supported
        configuration, not a missing one."""
        assert Settings(anthropic_api_key="test").database_url is None

    def test_asyncpg_url_is_accepted(self) -> None:
        url = "postgresql+asyncpg://reviewhive:reviewhive@localhost:5432/reviewhive"
        assert Settings(anthropic_api_key="test", database_url=url).database_url == url

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_url_means_unset_rather_than_invalid(self, blank: str) -> None:
        """`REVIEWHIVE_DATABASE_URL=` is how you turn persistence off for a single
        run without editing .env. Rejecting it would make the obvious gesture an
        error."""
        assert Settings(anthropic_api_key="test", database_url=blank).database_url is None

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://reviewhive@localhost/reviewhive",
            "postgresql+psycopg2://reviewhive@localhost/reviewhive",
            "sqlite+aiosqlite:///./local.db",
        ],
    )
    def test_a_non_asyncpg_url_is_rejected_at_load(self, url: str) -> None:
        """Caught here rather than at first query. The engine is async, and a sync
        URL otherwise surfaces as a greenlet error from inside SQLAlchemy that
        reads like a bug in this project."""
        with pytest.raises(ValidationError, match="asyncpg"):
            Settings(anthropic_api_key="test", database_url=url)
