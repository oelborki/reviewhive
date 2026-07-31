"""Runtime configuration, loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    enable_llm_merge: bool = True
    # Findings within this many lines of each other are candidates for the same issue.
    dedupe_line_tolerance: int = 3
    # Jaccard overlap on title tokens above which two findings collapse without an LLM call.
    dedupe_title_similarity: float = 0.5

    # --- Agent call behaviour ---
    agent_max_tokens: int = 8_000
    agent_timeout_seconds: float = 120.0
    agent_max_retries: int = 2


@lru_cache
def get_settings() -> Settings:
    return Settings()
