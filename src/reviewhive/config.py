"""Runtime configuration, loaded from environment / .env."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

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
    # On. `graph/llm_merge.py` reads this, and the pass costs one cheap call to
    # collapse the cross-lane duplicates the deterministic pass is documented as
    # leaving behind. Turning it off is a supported way to save that call; the
    # review is then exactly what it was before the pass existed.
    enable_llm_merge: bool = True
    # Findings within this many lines of each other are candidates for the same issue.
    dedupe_line_tolerance: int = 3
    # Jaccard overlap on title tokens above which two findings collapse without an LLM call.
    dedupe_title_similarity: float = 0.5

    # Wider than `dedupe_line_tolerance`, deliberately. The deterministic pass has
    # only a title to go on, so it must stay close to be safe; the merge pass reads
    # both bodies and can be trusted further out. The measured cross-lane pair sat
    # one line apart, but an architecture finding anchored at a function signature
    # and a security finding on the offending line inside it can be several.
    merge_line_window: int = 8
    # A cost guard, not a correctness one: pairs grow quadratically with findings
    # on one file, and a defect-dense diff would otherwise send a very large call.
    merge_max_pairs: int = 24

    # --- Agent call behaviour ---
    agent_max_tokens: int = 8_000
    agent_timeout_seconds: float = 120.0
    agent_max_retries: int = 2

    # --- GitHub ---
    # Prefixed rather than aliased to the bare GITHUB_TOKEN / GITHUB_WEBHOOK_SECRET.
    # `anthropic_api_key` is aliased because the Anthropic SDK reads that variable, so
    # the alias buys real interoperability. Nothing reads GITHUB_TOKEN here — httpx
    # reads no environment — and GitHub Actions injects that name into every job, so a
    # bare alias would hand CI's own token to a suite whose whole point is that it has
    # no credentials.
    github_token: str | None = None
    github_webhook_secret: str | None = None

    # Empty denies everything. With a public smee channel forwarding to a laptop,
    # allow-by-default means anyone who guesses the URL spends the Anthropic budget.
    allowed_repos: Annotated[frozenset[str], NoDecode] = frozenset()

    github_api_url: str = "https://api.github.com"
    github_timeout_seconds: float = 30.0

    # Unlike `enable_llm_merge`, this one is read on every delivery. Off because
    # `synchronize` fires per push with a fresh head sha, so the head-sha idempotency
    # cannot collapse a five-commit burst — that is five reviews and five bills.
    review_on_synchronize: bool = False

    # The literal string that summons the bot in a comment.
    #
    # Deliberately not an `@name`. This was `@reviewhive` on the reasoning that
    # the bot acts as a personal access token and so notifies nobody — which was
    # wrong twice over. `ReviewHive` is a real GitHub account, registered
    # 2025-12-27 by someone unconnected to this project, so every demo comment
    # linked to a stranger's profile. It notified nobody only because the demo
    # repository is private; making it public, which is the entire point of a
    # portfolio repository, would have turned each one into a real ping.
    #
    # A leading slash cannot be a GitHub username, so this can never collide with
    # an account that exists now or is registered later. It also reads as a
    # command, which is what it is.
    mention_handle: str = "/reviewhive"

    # Anyone who can comment can spend money. The self-login guard stops the bot
    # answering itself; this stops a person, or a loop nobody predicted, doing it
    # repeatedly on one pull request.
    max_mention_responses_per_hour: int = 10

    # --- Logging ---
    # Applies to the `reviewhive` loggers only. Third-party libraries stay at
    # WARNING whatever this says, so DEBUG here does not turn on an httpx line
    # per request. See `logging_setup.configure_logging`.
    log_level: str = "INFO"

    # --- Persistence ---
    # Unset means "do not persist". The CLI is the prompt-iteration loop and has to
    # keep working with no database running, so this is optional rather than
    # required, and a missing value is a supported configuration, not an error.
    database_url: str | None = None

    @field_validator("allowed_repos", mode="before")
    @classmethod
    def _split_repos(cls, value: object) -> frozenset[str]:
        # `NoDecode` above is what makes this reachable. Without it pydantic-settings
        # runs `json.loads` on the raw environment value for a complex type *before*
        # any validator, so `a/b,c/d` raises SettingsError and this never runs.
        if isinstance(value, str):
            value = value.split(",")
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError("allowed_repos must be a comma-separated string or a sequence")

        repos = frozenset(str(part).strip().lower() for part in value if str(part).strip())
        for repo in repos:
            # Checked at load, not at first delivery. A typo here otherwise 403s every
            # webhook and sends you hunting through the plumbing instead of the config.
            if repo.count("/") != 1 or " " in repo or repo.startswith("/") or repo.endswith("/"):
                raise ValueError(f"allowed_repos entries must be owner/repo, got {repo!r}")
        return repos

    @field_validator("log_level")
    @classmethod
    def _known_level(cls, value: str) -> str:
        # Checked at load for the same reason `allowed_repos` is: `setLevel`
        # raises on an unknown name, and the only call site is inside lifespan,
        # so a typo would otherwise surface as a service that will not boot with
        # a traceback pointing at the logging module rather than at the config.
        level = value.strip().upper()
        if level not in logging.getLevelNamesMapping():
            raise ValueError(
                f"log_level must be a Python logging level name, got {value!r}. "
                f"Use one of DEBUG, INFO, WARNING, ERROR, CRITICAL."
            )
        return level

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
