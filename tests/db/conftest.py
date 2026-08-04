"""Fixtures for tests that need a real Postgres.

These live in their own directory rather than beside the other integration tests
because the `importorskip` below is module-level: in a shared conftest it would
skip the offline graph tests too, quietly making them depend on the database
extra.

Everything here is deselected by default (`-m 'not db'` in pyproject), so a bare
`pytest` never imports this file.

    docker compose up -d db
    pytest -m db
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator

import pytest

pytest.importorskip("sqlalchemy", exc_type=ImportError, reason="requires the db extra")

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from reviewhive.db.repository import SqlReviewStore
from reviewhive.db.session import create_engine, make_sessionmaker

TEST_DATABASE = "reviewhive_test"
URL_VAR = "REVIEWHIVE_TEST_DATABASE_URL"

# Note: `pytestmark` has no effect in a conftest — it only applies at module or
# class level. Each test module here declares `pytestmark = pytest.mark.db`
# itself, and the marker is what the default `-m 'not db'` deselects.


class _TestDatabaseSettings(BaseSettings):
    """Reads the test URL from the environment or .env.

    Its own class rather than the application's `Settings`, for two reasons. The
    parent conftest deliberately scrubs `REVIEWHIVE_*` and disables the .env file
    for every test, which is right for all of them and would leave this one with
    nothing. And the URL genuinely is test-only configuration — putting it on the
    application settings would imply the application should know about it.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    REVIEWHIVE_TEST_DATABASE_URL: str | None = None


def _url() -> str:
    """The test database URL, or skip."""
    url = os.environ.get(URL_VAR) or _TestDatabaseSettings().REVIEWHIVE_TEST_DATABASE_URL
    if not url:
        pytest.skip(
            f"{URL_VAR} is not set (checked the environment and .env); "
            f"start Postgres with `docker compose up -d db`"
        )

    # These tests TRUNCATE between cases. Refusing any database not named
    # reviewhive_test means a URL copied from the development configuration
    # cannot quietly empty real data.
    if url.rsplit("/", 1)[-1] != TEST_DATABASE:
        pytest.fail(f"{URL_VAR} must name the {TEST_DATABASE!r} database, got {url!r}")
    return url


@pytest.fixture(scope="session")
def migrated_database() -> str:
    """Bring the schema up once per session by running the migration.

    Running the migration rather than `metadata.create_all()` is the whole point:
    if the tests built the schema themselves, the migration would never execute
    here and could drift from the models indefinitely, with the first person to
    deploy finding out.

    In a subprocess because Alembic's async env.py calls `asyncio.run()`
    internally, which raises when a loop is already running in this process.
    """
    url = _url()
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        env={**os.environ, "REVIEWHIVE_DATABASE_URL": url},
    )
    if completed.returncode != 0:
        pytest.fail(f"alembic upgrade head failed:\n{completed.stdout}\n{completed.stderr}")
    return url


@pytest.fixture
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    """A fresh engine per test.

    Function-scoped deliberately. asyncpg connections bind to the loop that
    created them and pytest-asyncio gives each test its own, so a session-scoped
    engine would hand out connections belonging to a closed loop and fail in a way
    that reads as flakiness rather than as a fixture-scope mistake.
    """
    engine = create_engine(migrated_database)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def store(engine: AsyncEngine) -> AsyncIterator[SqlReviewStore]:
    """A store on an empty database.

    Truncated rather than wrapped in a rolled-back transaction: the code under
    test commits, and a savepoint dance would be fighting the exact behaviour
    these tests exist to verify.
    """
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE reviews CASCADE"))

    yield SqlReviewStore(make_sessionmaker(engine))
