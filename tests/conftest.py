"""Shared test fixtures.

Nothing in this suite touches the network. `ANTHROPIC_API_KEY` is deliberately
cleared so that an accidental real client construction fails loudly rather than
quietly spending money.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


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
