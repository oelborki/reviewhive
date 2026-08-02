"""Engine and session lifecycle.

Split into three pieces rather than one helper because two callers want different
things from it. The CLI runs once and wants the context manager: build an engine,
use it, dispose of it. A long-lived service wants to own the engine for the
process lifetime and hand sessionmakers to individual jobs, because a background
task that outlives the response it was dispatched from cannot share a session
with it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from reviewhive.db.repository import SqlReviewStore


def create_engine(url: str) -> AsyncEngine:
    # pool_pre_ping because a container restart or an idle timeout otherwise
    # surfaces as a dead connection on the next query rather than a reconnect.
    return create_async_engine(url, pool_pre_ping=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    # expire_on_commit=False so a row stays readable after the transaction that
    # wrote it commits; the default would re-fetch on every attribute access and
    # fail once the session is closed.
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def review_store(url: str) -> AsyncIterator[SqlReviewStore]:
    """A store backed by its own engine, disposed on exit.

    For a process that runs one review and exits. A service should build the
    engine once at startup instead and keep it.
    """
    engine = create_engine(url)
    try:
        yield SqlReviewStore(make_sessionmaker(engine))
    finally:
        await engine.dispose()
