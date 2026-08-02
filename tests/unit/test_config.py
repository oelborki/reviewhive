from __future__ import annotations

import pytest
from pydantic import ValidationError

from reviewhive.config import Settings


class TestDatabaseUrl:
    def test_unset_is_valid_and_means_do_not_persist(self) -> None:
        """The CLI has to run with no database. An absent URL is a supported
        configuration, not a missing one."""
        assert Settings(anthropic_api_key="test").database_url is None

    def test_asyncpg_url_is_accepted(self) -> None:
        url = "postgresql+asyncpg://reviewhive:reviewhive@localhost:5432/reviewhive"
        assert Settings(anthropic_api_key="test", database_url=url).database_url == url

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
