"""Shared test fixtures.

Nothing in this suite touches the network. `ANTHROPIC_API_KEY` is deliberately
cleared so that an accidental real client construction fails loudly rather than
quietly spending money.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from reviewhive.config import Settings, get_settings

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Detach every test from the developer's local configuration.

    `Settings` draws from two sources, and both have to be cut or the isolation is
    only apparent. Environment variables are the obvious one: the API key goes so
    an accidental real client fails loudly rather than quietly spending money, and
    every `REVIEWHIVE_*` goes so a local value cannot change a budget out from
    under an assertion.

    The `.env` *file* is the one that bites. Clearing environment variables does
    nothing to it, so a developer who fills in `.env` — which the README tells
    them to do — silently changes what the suite is testing. Point `env_file` at
    nothing for the duration instead.

    `get_settings` is `lru_cache`d, so the cache has to be dropped either side or
    none of this is visible to it.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for name in [k for k in os.environ if k.startswith("REVIEWHIVE_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(Settings.model_config, "env_file", None)

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def diff_text() -> str:
    return (FIXTURES / "diffs" / "mixed.diff").read_text(encoding="utf-8")


@pytest.fixture
def count_chars():
    """Deterministic stand-in for `messages.count_tokens`.

    Roughly four characters per token, which is close enough for budget tests and
    keeps them offline and instant.
    """

    async def _count(text: str) -> int:
        return max(1, len(text) // 4)

    return _count
