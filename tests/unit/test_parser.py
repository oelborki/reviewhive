from __future__ import annotations

import pytest

from reviewhive.diff.parser import DiffFile, DiffHunk, parse_diff
from tests.conftest import FIXTURES

DIFF_FIXTURES = sorted((FIXTURES / "diffs").glob("*.diff"))


@pytest.mark.parametrize("path", DIFF_FIXTURES, ids=lambda p: p.name)
def test_every_fixture_diff_parses(path) -> None:
    """A fixture with a miscounted hunk header parses to nothing, and a review of
    nothing returns no findings without erroring — so a broken fixture reads as a
    clean diff. Generate these with `git diff` rather than by hand, and keep this
    test as the tripwire."""
    parsed = parse_diff(path.read_text(encoding="utf-8"))

    assert parsed.unparseable == [], f"{path.name} has a malformed hunk header"
    assert parsed.files, f"{path.name} produced no reviewable files"


def test_parses_every_file_entry(diff_text: str) -> None:
    parsed = parse_diff(diff_text)

    assert parsed.unparseable == []
    assert [f.path for f in parsed.files] == [
        "src/app/auth.py",
        "package-lock.json",
        "assets/logo.png",
        "src/app/helpers.py",
        "src/app/legacy.py",
    ]


def test_added_line_numbers_are_new_file_positions(diff_text: str) -> None:
    auth = next(f for f in parse_diff(diff_text).files if f.path == "src/app/auth.py")

    # Second hunk starts at 44; the injected token lands on 47.
    assert sorted(auth.added_lines) == [13, 16, 17, 18, 19, 47, 48]
    assert 47 in auth.anchorable_lines
    # A line outside both hunks is not a legal comment anchor.
    assert 30 not in auth.anchorable_lines


def test_classifies_special_entries(diff_text: str) -> None:
    by_path = {f.path: f for f in parse_diff(diff_text).files}

    assert by_path["assets/logo.png"].is_binary
    assert by_path["assets/logo.png"].hunks == ()
    assert by_path["src/app/helpers.py"].is_rename
    assert by_path["src/app/helpers.py"].old_path == "src/app/old_utils.py"
    assert by_path["src/app/legacy.py"].is_deletion


def test_empty_diff_yields_nothing() -> None:
    assert parse_diff("").files == []
    assert parse_diff("   \n").files == []


def test_malformed_entry_is_isolated_not_fatal(diff_text: str) -> None:
    """One unreadable file must not take down the rest of the review."""
    broken = (
        "diff --git a/broken.py b/broken.py\n"
        "--- a/broken.py\n"
        "+++ b/broken.py\n"
        "@@ -1,99 +1,99 @@\n"  # header promises 99 lines, body has one
        "+oops\n"
    )
    parsed = parse_diff(broken + diff_text)

    assert "broken.py" in parsed.unparseable
    assert "src/app/auth.py" in [f.path for f in parsed.files]


class TestNearestAnchor:
    def _file(self, added: set[int], context: set[int] | None = None) -> DiffFile:
        hunk = DiffHunk(
            text="",
            added_lines=frozenset(added),
            context_lines=frozenset(context or ()),
            removed_lines=frozenset(),
        )
        return DiffFile(path="f.py", old_path=None, header="", hunks=(hunk,))

    def test_exact_hit_is_returned_unchanged(self) -> None:
        assert self._file({10, 20}).nearest_anchor(10) == 10

    def test_near_miss_snaps_to_closest(self) -> None:
        assert self._file({10, 20}).nearest_anchor(12) == 10

    def test_added_lines_win_over_equidistant_context(self) -> None:
        # 8 is context, 12 is added; both are 2 away from 10.
        assert self._file(added={12}, context={8}).nearest_anchor(10) == 12

    def test_far_miss_gives_up_rather_than_guessing(self) -> None:
        assert self._file({10}).nearest_anchor(400) is None

    def test_file_with_no_anchors_gives_up(self) -> None:
        assert DiffFile(path="f.py", old_path=None, header="").nearest_anchor(1) is None
