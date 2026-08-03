"""The application, and what it owns for the life of the process.

`create_app(deps=...)` is the whole test story. Passing dependencies in skips
construction *and* skips tearing down what this module did not create, so a test
assembles the real app around a stub client, an in-memory store and a stub
transport with nothing patched — the same convention `build_review_graph` uses.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from anthropic import AsyncAnthropic
from fastapi import FastAPI, Request

from reviewhive.api.webhook import router
from reviewhive.config import Settings, get_settings
from reviewhive.github.client import GitHubClient
from reviewhive.graph.build import build_review_graph
from reviewhive.jobs import JobDeps
from reviewhive.persistence import NullReviewStore

logger = logging.getLogger(__name__)


class MisconfiguredService(RuntimeError):
    """The service cannot safely start."""


def _require(settings: Settings) -> None:
    """Refuse to start rather than fail at the first delivery.

    A missing webhook secret is the one that matters: an endpoint that verifies
    nothing looks perfectly healthy while accepting anything, and every request
    it accepts spends money. An empty allowlist is loud for the same reason —
    it is more likely to be a forgotten setting than a deliberate one.
    """
    missing = [
        name
        for name, value in (
            ("REVIEWHIVE_GITHUB_WEBHOOK_SECRET", settings.github_webhook_secret),
            ("REVIEWHIVE_GITHUB_TOKEN", settings.github_token),
        )
        if not value
    ]
    if missing:
        raise MisconfiguredService(f"cannot start without {', '.join(missing)}")
    if not settings.allowed_repos:
        raise MisconfiguredService(
            "REVIEWHIVE_ALLOWED_REPOS is empty, so every delivery would be rejected; "
            "set it to the repositories this service should review"
        )


async def _build_deps(settings: Settings) -> tuple[JobDeps, list]:
    """Construct what the process owns, and how to close it."""
    _require(settings)

    client = AsyncAnthropic(
        api_key=settings.anthropic_api_key,
        timeout=settings.agent_timeout_seconds,
        max_retries=settings.agent_max_retries,
    )
    github = GitHubClient(
        token=settings.github_token or "",
        base_url=settings.github_api_url,
        timeout=settings.github_timeout_seconds,
    )

    closers = [client.close, github.aclose]
    store = NullReviewStore()

    if settings.database_url:
        # Imported lazily so the service still starts without the `db` extra when
        # no database is configured.
        from reviewhive.db.repository import SqlReviewStore
        from reviewhive.db.session import create_engine, make_sessionmaker

        engine = create_engine(settings.database_url)
        # One store for the process. It holds a sessionmaker rather than a
        # session and opens one per call, so it is safe across the concurrent
        # background tasks that outlive the requests dispatching them.
        store = SqlReviewStore(make_sessionmaker(engine))
        closers.append(engine.dispose)

    # Resolved before the deps are frozen. It is what stops the bot answering its
    # own comments; a failure here is logged rather than fatal, because a service
    # that will not start is worse than one that cannot recognise itself — but the
    # mention handler treats an unknown login as "guard unavailable".
    try:
        self_login = await github.whoami()
    except Exception:
        logger.exception("could not determine the bot's own login")
        self_login = None

    deps = JobDeps(
        settings=settings,
        graph=build_review_graph(client, settings),
        github=github,
        store=store,
        client=client,
        self_login=self_login,
    )
    return deps, closers


def create_app(deps: JobDeps | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if deps is not None:
            # Injected: this module owns nothing, so it closes nothing.
            app.state.deps = deps
            yield
            return

        built, closers = await _build_deps(get_settings())
        app.state.deps = built
        try:
            yield
        finally:
            for close in reversed(closers):
                try:
                    await close()
                except Exception:
                    logger.exception("error during shutdown")

    app = FastAPI(title="reviewHive", lifespan=lifespan)
    app.include_router(router)
    return app


def get_deps(request: Request) -> JobDeps:
    """The process-wide dependencies.

    Read off `app.state` through one helper rather than through `Depends`, which
    would add a layer of machinery for a value that never varies per request.
    """
    return request.app.state.deps


app = create_app()
