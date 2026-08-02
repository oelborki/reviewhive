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
