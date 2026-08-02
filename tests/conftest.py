"""Shared test fixtures.

Nothing in this suite touches the network. `ANTHROPIC_API_KEY` is deliberately
cleared so that an accidental real client construction fails loudly rather than
quietly spending money.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from reviewhive.config import get_settings

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Detach every test from whatever is in the developer's `.env`.

    The API key is cleared so an accidental real client fails loudly rather than
    quietly spending money. Every `REVIEWHIVE_*` variable goes too, so a local
    `.env` cannot point a test at a live database or change a budget out from
    under an assertion. `get_settings` is `lru_cache`d, so its cache has to be
    dropped either side of the change or the scrubbing is invisible to it.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for name in [k for k in os.environ if k.startswith("REVIEWHIVE_")]:
        monkeypatch.delenv(name, raising=False)

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
