"""Unified-diff parsing.

Wraps `unidiff` rather than hand-rolling regex. The one behaviour we add on top is
fault tolerance: `unidiff` parses a patch as a single unit, so one malformed file
entry would otherwise take down the whole review. We split on `diff --git`
boundaries and re-parse individually, so a file we cannot understand becomes a
skipped file rather than an exception.

`anchorable_lines` is the load-bearing output. GitHub only accepts a review comment
on a line that appears in the diff, so Phase 3 validates every anchor against this
set before sending — a mismatch is a 422 for the *entire* review request, not just
the offending comment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from unidiff import PatchSet
from unidiff.errors import UnidiffParseError

_FILE_BOUNDARY = re.compile(r"^diff --git ", re.MULTILINE)


@dataclass(frozen=True)
class DiffHunk:
    """A single `@@` block. Truncation operates at this granularity so that a
    shortened file is still a structurally valid diff and its line bookkeeping
    stays exact."""

    text: str
    added_lines: frozenset[int]
    context_lines: frozenset[int]
    removed_lines: frozenset[int]

    @property
    def line_count(self) -> int:
        return len(self.added_lines) + len(self.context_lines) + len(self.removed_lines)


@dataclass(frozen=True)
class DiffFile:
    """One file's worth of changes, plus the line bookkeeping Phase 3 needs."""

    path: str
    old_path: str | None
    header: str
    hunks: tuple[DiffHunk, ...] = ()
    is_binary: bool = False
    is_rename: bool = False
    is_addition: bool = False
    is_deletion: bool = False
    omitted_hunks: int = 0
    omitted_lines: int = 0

    @property
    def text(self) -> str:
        """The diff exactly as the model will see it, including any omission marker."""
        parts = [self.header, *(h.text for h in self.hunks)]
        body = "".join(parts)
        if self.omitted_hunks:
            body += (
                f"\n[reviewHive: {self.omitted_lines} further lines across "
                f"{self.omitted_hunks} hunk(s) omitted to stay within the review budget]\n"
            )
        return body

    @property
    def truncated(self) -> bool:
        return self.omitted_hunks > 0

    @property
    def added_lines(self) -> frozenset[int]:
        return (
            frozenset().union(*(h.added_lines for h in self.hunks)) if self.hunks else frozenset()
        )

    @property
    def context_lines(self) -> frozenset[int]:
        return (
            frozenset().union(*(h.context_lines for h in self.hunks)) if self.hunks else frozenset()
        )

    @property
    def removed_lines(self) -> frozenset[int]:
        return (
            frozenset().union(*(h.removed_lines for h in self.hunks)) if self.hunks else frozenset()
        )

    @property
    def anchorable_lines(self) -> frozenset[int]:
        """New-file line numbers a review comment may be attached to (RIGHT side)."""
        return self.added_lines | self.context_lines

    @property
    def changed_line_count(self) -> int:
        return len(self.added_lines) + len(self.removed_lines)

    def truncated_to(self, max_lines: int) -> DiffFile:
        """Return a copy keeping whole hunks up to `max_lines` of diff content.

        At least one hunk is always kept — reviewing a fragment beats reviewing
        nothing, and a file whose very first hunk blows the budget is exactly the
        kind of file a reviewer most wants flagged.
        """
        kept: list[DiffHunk] = []
        used = 0
        for hunk in self.hunks:
            if kept and used + hunk.line_count > max_lines:
                break
            kept.append(hunk)
            used += hunk.line_count

        if len(kept) == len(self.hunks):
            return self

        dropped = self.hunks[len(kept) :]
        return replace(
            self,
            hunks=tuple(kept),
            omitted_hunks=self.omitted_hunks + len(dropped),
            omitted_lines=self.omitted_lines + sum(h.line_count for h in dropped),
        )

    def nearest_anchor(self, line: int, tolerance: int = 5) -> int | None:
        """Snap a model-reported line to the closest real anchor, or give up.

        Models are usually right about the line but occasionally off by one or two
        after a hunk boundary. Snapping recovers those; anything further away is
        more likely a hallucinated location than an off-by-one, so we return None
        and let the caller degrade the finding into the summary.
        """
        anchors = self.anchorable_lines
        if not anchors:
            return None
        if line in anchors:
            return line
        # Prefer added lines — a comment on code the PR actually introduced reads
        # better than one on untouched context that merely happens to be nearby.
        for candidate_set in (self.added_lines, anchors):
            nearby = [n for n in candidate_set if abs(n - line) <= tolerance]
            if nearby:
                return min(nearby, key=lambda n: (abs(n - line), n))
        return None


@dataclass
class ParsedDiff:
    files: list[DiffFile] = field(default_factory=list)
    unparseable: list[str] = field(default_factory=list)


def parse_diff(diff_text: str) -> ParsedDiff:
    """Parse a unified diff, degrading gracefully on entries we cannot read."""
    if not diff_text.strip():
        return ParsedDiff()

    try:
        return ParsedDiff(files=[_to_diff_file(pf) for pf in PatchSet(diff_text)])
    except (UnidiffParseError, UnicodeDecodeError, ValueError):
        return _parse_per_file(diff_text)


def _parse_per_file(diff_text: str) -> ParsedDiff:
    """Fallback: isolate each file entry so one bad entry cannot poison the rest."""
    result = ParsedDiff()
    for chunk in _split_file_entries(diff_text):
        try:
            patch = PatchSet(chunk)
        except (UnidiffParseError, UnicodeDecodeError, ValueError):
            result.unparseable.append(_guess_path(chunk))
            continue
        for patched_file in patch:
            try:
                result.files.append(_to_diff_file(patched_file))
            except (UnidiffParseError, ValueError):
                result.unparseable.append(_guess_path(chunk))
    return result


def _split_file_entries(diff_text: str) -> list[str]:
    boundaries = [m.start() for m in _FILE_BOUNDARY.finditer(diff_text)]
    if not boundaries:
        return [diff_text]
    bounds = [*boundaries, len(diff_text)]
    return [diff_text[bounds[i] : bounds[i + 1]] for i in range(len(bounds) - 1)]


def _guess_path(chunk: str) -> str:
    first_line = chunk.lstrip().split("\n", 1)[0]
    match = re.match(r"diff --git a/(.+?) b/(.+)$", first_line)
    return match.group(2) if match else "<unknown file>"


def _to_diff_file(patched_file) -> DiffFile:
    hunks = tuple(_to_hunk(h) for h in patched_file)

    old_path = _strip_prefix(patched_file.source_file)
    new_path = _strip_prefix(patched_file.target_file)
    path = new_path or old_path or "<unknown file>"

    return DiffFile(
        path=path,
        old_path=old_path if old_path != new_path else None,
        header=_header_of(patched_file),
        hunks=hunks,
        is_binary=bool(getattr(patched_file, "is_binary_file", False)),
        is_rename=bool(getattr(patched_file, "is_rename", False)),
        is_addition=bool(getattr(patched_file, "is_added_file", False)),
        is_deletion=bool(getattr(patched_file, "is_removed_file", False)),
    )


def _to_hunk(hunk) -> DiffHunk:
    added: set[int] = set()
    context: set[int] = set()
    removed: set[int] = set()

    for line in hunk:
        if line.is_added and line.target_line_no is not None:
            added.add(line.target_line_no)
        elif line.is_context and line.target_line_no is not None:
            context.add(line.target_line_no)
        elif line.is_removed and line.source_line_no is not None:
            removed.add(line.source_line_no)

    return DiffHunk(
        text=str(hunk),
        added_lines=frozenset(added),
        context_lines=frozenset(context),
        removed_lines=frozenset(removed),
    )


def _header_of(patched_file) -> str:
    """Everything before the first `@@`: the `diff --git`, mode, index and ---/+++ lines."""
    rendered = str(patched_file)
    marker = rendered.find("@@")
    return rendered if marker == -1 else rendered[:marker]


def _strip_prefix(path: str | None) -> str | None:
    """Turn `a/src/foo.py` into `src/foo.py`; `/dev/null` into None."""
    if not path or path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path
