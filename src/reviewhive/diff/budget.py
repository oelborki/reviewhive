"""Diff filtering and token budgeting.

The goal is that a pathological PR degrades *visibly* rather than silently. Every
file we drop or shorten is recorded and ends up in the posted summary, so a human
reading the review always knows what the bot did and did not look at.

Token counts come from `messages.count_tokens`, never from a local estimator —
`tiktoken` is OpenAI's tokenizer and materially undercounts Claude tokens on code.
The counter is injected so tests run offline.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from fnmatch import fnmatch

from reviewhive.diff.parser import DiffFile, ParsedDiff

TokenCounter = Callable[[str], Awaitable[int]]

# Paths that cost tokens and yield nothing worth saying. Matched against the full
# repo-relative path and, for the bare-name patterns, the basename.
NOISE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("*.lock", "lockfile"),
    ("package-lock.json", "lockfile"),
    ("yarn.lock", "lockfile"),
    ("pnpm-lock.yaml", "lockfile"),
    ("poetry.lock", "lockfile"),
    ("Cargo.lock", "lockfile"),
    ("go.sum", "lockfile"),
    ("*.min.js", "minified"),
    ("*.min.css", "minified"),
    ("*.map", "source map"),
    ("*.snap", "test snapshot"),
    ("*.pb.go", "generated"),
    ("*_pb2.py", "generated"),
    ("*_pb2_grpc.py", "generated"),
    ("*.g.dart", "generated"),
    ("*.generated.*", "generated"),
    ("vendor/*", "vendored"),
    ("*/vendor/*", "vendored"),
    ("node_modules/*", "vendored"),
    ("*/node_modules/*", "vendored"),
    ("dist/*", "build output"),
    ("*/dist/*", "build output"),
    ("build/*", "build output"),
    ("*/build/*", "build output"),
    ("*.svg", "asset"),
    ("*.png", "asset"),
    ("*.jpg", "asset"),
    ("*.ico", "asset"),
    ("*.woff*", "asset"),
)


@dataclass
class BudgetedDiff:
    """What the agents actually receive, plus a record of what was left out."""

    files: list[DiffFile] = field(default_factory=list)
    prompt_text: str = ""
    token_count: int = 0
    skipped: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.files


def classify_noise(path: str) -> str | None:
    """Return a human-readable reason if this path is not worth reviewing."""
    basename = path.rsplit("/", 1)[-1]
    for pattern, reason in NOISE_PATTERNS:
        if fnmatch(path, pattern) or fnmatch(basename, pattern):
            return reason
    return None


def render_prompt(files: list[DiffFile]) -> str:
    return "\n".join(f.text for f in files)


async def build_budget(
    parsed: ParsedDiff,
    count_tokens: TokenCounter,
    *,
    max_prompt_tokens: int,
    max_file_diff_lines: int,
) -> BudgetedDiff:
    """Filter, truncate, and cap a parsed diff so it fits the prompt budget."""
    result = BudgetedDiff()
    result.skipped.extend(f"{path} (could not parse)" for path in parsed.unparseable)

    kept: list[DiffFile] = []
    for diff_file in parsed.files:
        if diff_file.is_binary:
            result.skipped.append(f"{diff_file.path} (binary)")
            continue
        if reason := classify_noise(diff_file.path):
            result.skipped.append(f"{diff_file.path} ({reason})")
            continue
        if not diff_file.hunks:
            # Mode change, empty rename, or an entry with no content to review.
            result.skipped.append(f"{diff_file.path} (no reviewable changes)")
            continue

        shortened = diff_file.truncated_to(max_file_diff_lines)
        if shortened.truncated:
            result.truncated.append(f"{shortened.path} ({shortened.omitted_lines} lines omitted)")
        kept.append(shortened)

    if not kept:
        return result

    # Common case: everything fits, one token count and we are done.
    tokens = await count_tokens(render_prompt(kept))
    if tokens > max_prompt_tokens:
        kept, tokens, dropped = await _fit_to_budget(kept, count_tokens, max_prompt_tokens)
        result.skipped.extend(f"{path} (exceeds review token budget)" for path in dropped)

    result.files = sorted(kept, key=lambda f: f.path)
    result.prompt_text = render_prompt(result.files)
    result.token_count = tokens
    return result


async def _fit_to_budget(
    files: list[DiffFile],
    count_tokens: TokenCounter,
    max_prompt_tokens: int,
) -> tuple[list[DiffFile], int, list[str]]:
    """Drop whole files, largest first, until the prompt fits.

    Largest-first frees the most budget per file dropped, so more files survive
    overall — one huge generated file that slipped the filter costs one entry, not
    twenty. Per-file counts are measured concurrently, once, rather than
    re-counting the whole prompt after every drop.
    """
    sizes = await asyncio.gather(*(count_tokens(f.text) for f in files))
    by_size = sorted(zip(files, sizes, strict=True), key=lambda pair: pair[1], reverse=True)

    total = sum(sizes)
    dropped: list[str] = []
    # Always keep at least one file: reviewing a fragment beats reviewing nothing,
    # and a single file that alone blows the budget is exactly what a human most
    # wants flagged.
    while total > max_prompt_tokens and len(by_size) > 1:
        diff_file, size = by_size.pop(0)
        dropped.append(diff_file.path)
        total -= size

    return [f for f, _ in by_size], total, dropped
