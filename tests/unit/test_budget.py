from __future__ import annotations

import pytest

from reviewhive.diff.budget import build_budget, classify_noise
from reviewhive.diff.parser import DiffFile, DiffHunk, ParsedDiff, parse_diff


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("package-lock.json", "lockfile"),
        ("poetry.lock", "lockfile"),
        ("web/yarn.lock", "lockfile"),
        ("go.sum", "lockfile"),
        ("static/app.min.js", "minified"),
        ("api/service_pb2.py", "generated"),
        ("vendor/github.com/x/y.go", "vendored"),
        ("web/node_modules/left-pad/index.js", "vendored"),
        ("assets/logo.svg", "asset"),
        ("src/app/auth.py", None),
        ("src/lockfile_utils.py", None),
        ("src/distributed.py", None),
    ],
)
def test_noise_classification(path: str, expected: str | None) -> None:
    assert classify_noise(path) == expected


async def test_filters_noise_and_binary_but_keeps_source(diff_text, count_chars) -> None:
    budget = await build_budget(
        parse_diff(diff_text),
        count_chars,
        max_prompt_tokens=100_000,
        max_file_diff_lines=400,
    )

    assert [f.path for f in budget.files] == [
        "src/app/auth.py",
        "src/app/helpers.py",
        "src/app/legacy.py",
    ]
    assert "package-lock.json (lockfile)" in budget.skipped
    assert "assets/logo.png (binary)" in budget.skipped


async def test_skips_are_reported_not_silent(diff_text, count_chars) -> None:
    """Every exclusion must be visible to whoever reads the posted review."""
    budget = await build_budget(
        parse_diff(diff_text),
        count_chars,
        max_prompt_tokens=100_000,
        max_file_diff_lines=400,
    )

    reviewed = {f.path for f in budget.files}
    reported = {entry.split(" (")[0] for entry in budget.skipped}
    all_paths = {f.path for f in parse_diff(diff_text).files}

    assert reviewed | reported == all_paths


def _file(path: str, hunk_count: int, lines_per_hunk: int = 10) -> DiffFile:
    hunks = tuple(
        DiffHunk(
            text=f"@@ hunk {i} of {path} @@\n" + ("+line\n" * lines_per_hunk),
            added_lines=frozenset(range(i * 100, i * 100 + lines_per_hunk)),
            context_lines=frozenset(),
            removed_lines=frozenset(),
        )
        for i in range(hunk_count)
    )
    return DiffFile(path=path, old_path=None, header=f"--- a/{path}\n+++ b/{path}\n", hunks=hunks)


class TestTruncation:
    def test_keeps_whole_hunks_within_limit(self) -> None:
        shortened = _file("big.py", hunk_count=5, lines_per_hunk=10).truncated_to(25)

        assert len(shortened.hunks) == 2
        assert shortened.omitted_hunks == 3
        assert shortened.omitted_lines == 30

    def test_always_keeps_at_least_one_hunk(self) -> None:
        shortened = _file("huge.py", hunk_count=3, lines_per_hunk=500).truncated_to(10)

        assert len(shortened.hunks) == 1
        assert shortened.truncated

    def test_untruncated_file_is_returned_identically(self) -> None:
        original = _file("small.py", hunk_count=2, lines_per_hunk=5)
        assert original.truncated_to(1000) is original

    def test_omission_marker_appears_in_model_visible_text(self) -> None:
        text = _file("big.py", hunk_count=5).truncated_to(25).text
        assert "omitted to stay within the review budget" in text

    def test_anchors_shrink_with_the_text(self) -> None:
        """A finding cannot legitimately anchor to a hunk the model never saw."""
        original = _file("big.py", hunk_count=5)
        shortened = original.truncated_to(25)

        assert shortened.anchorable_lines < original.anchorable_lines
        assert 400 in original.anchorable_lines  # hunk 4
        assert 400 not in shortened.anchorable_lines


class TestPromptBudget:
    async def test_drops_largest_file_first(self, count_chars) -> None:
        parsed = ParsedDiff(
            files=[
                _file("small.py", hunk_count=1),
                _file("enormous.py", hunk_count=60),
                _file("medium.py", hunk_count=3),
            ]
        )
        budget = await build_budget(
            parsed, count_chars, max_prompt_tokens=200, max_file_diff_lines=10_000
        )

        assert [f.path for f in budget.files] == ["medium.py", "small.py"]
        assert "enormous.py (exceeds review token budget)" in budget.skipped

    async def test_single_oversized_file_is_still_reviewed(self, count_chars) -> None:
        parsed = ParsedDiff(files=[_file("only.py", hunk_count=50)])
        budget = await build_budget(
            parsed, count_chars, max_prompt_tokens=10, max_file_diff_lines=10_000
        )

        assert [f.path for f in budget.files] == ["only.py"]

    async def test_counts_tokens_once_when_everything_fits(self, diff_text) -> None:
        calls: list[str] = []

        async def counting(text: str) -> int:
            calls.append(text)
            return 10

        await build_budget(
            parse_diff(diff_text), counting, max_prompt_tokens=1000, max_file_diff_lines=400
        )

        assert len(calls) == 1

    async def test_unparseable_files_surface_in_skipped(self, count_chars) -> None:
        budget = await build_budget(
            ParsedDiff(files=[], unparseable=["broken.py"]),
            count_chars,
            max_prompt_tokens=1000,
            max_file_diff_lines=400,
        )

        assert budget.skipped == ["broken.py (could not parse)"]
        assert budget.is_empty
