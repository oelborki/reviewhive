"""Runtime configuration, loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Anything else — a bare `postgresql://`, or `psycopg2` — fails deep inside
# SQLAlchemy with an error about a missing greenlet, which reads as a bug in this
# project rather than a URL typo.
_ASYNC_DRIVER = "postgresql+asyncpg://"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="REVIEWHIVE_",
        extra="ignore",
    )

    # Read without the REVIEWHIVE_ prefix so the SDK's own convention still works.
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")

    agent_model: str = "claude-haiku-4-5"

    # --- Budget ---
    max_prompt_tokens: int = 60_000
    max_file_diff_lines: int = 400
    max_posted_findings: int = 15
    min_confidence: float = 0.35

    # --- Dedup ---
    # Off because there is no merge pass to enable. The deterministic collapse in
    # `graph/dedupe.py` is the whole of deduplication today; this flag reserves the
    # name for the second pass and must stay False until one exists, or it reads as
    # a feature that silently does nothing.
    enable_llm_merge: bool = False
    # Findings within this many lines of each other are candidates for the same issue.
    dedupe_line_tolerance: int = 3
    # Jaccard overlap on title tokens above which two findings collapse without an LLM call.
    dedupe_title_similarity: float = 0.5

    # --- Agent call behaviour ---
    agent_max_tokens: int = 8_000
    agent_timeout_seconds: float = 120.0
    agent_max_retries: int = 2

    # --- Persistence ---
    # Unset means "do not persist". The CLI is the prompt-iteration loop and has to
    # keep working with no database running, so this is optional rather than
    # required, and a missing value is a supported configuration, not an error.
    database_url: str | None = None

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: str | None) -> str | None:
        # Blank reads as unset. `REVIEWHIVE_DATABASE_URL=` is how anyone turns
        # persistence off for one run without editing .env, and rejecting it would
        # make the obvious gesture an error.
        if value is not None and not value.strip():
            return None
        if value is not None and not value.startswith(_ASYNC_DRIVER):
            raise ValueError(
                f"database_url must use the asyncpg driver, i.e. start with "
                f"{_ASYNC_DRIVER!r}. The engine is async; a sync URL fails much "
                f"later with an unrelated-looking greenlet error."
            )
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
