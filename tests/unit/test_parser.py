from __future__ import annotations

from collections import Counter

import pytest
from tests.conftest import FIXTURES

from reviewhive.diff.parser import DiffFile, DiffHunk, parse_diff

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


class TestNumberedRendering:
    """The gutter is what the model copies into `line`, so it has to agree with
    `anchorable_lines` exactly. If they can disagree, the prompt is inviting
    anchors that GitHub will reject — and a rejected anchor fails the whole review
    request, not just one comment."""

    @staticmethod
    def _gutter(file) -> list[int]:
        numbers = []
        for raw in file.numbered_text.splitlines():
            head = raw[:5].strip()
            if head.isdigit():
                numbers.append(int(head))
        return numbers

    @pytest.mark.parametrize("path", DIFF_FIXTURES, ids=lambda p: p.name)
    def test_gutter_numbers_are_exactly_the_anchorable_lines(self, path) -> None:
        for file in parse_diff(path.read_text(encoding="utf-8")).files:
            if not file.hunks:
                continue
            assert set(self._gutter(file)) == set(file.anchorable_lines), file.path

    def test_line_numbers_overlap_across_files_in_a_real_diff(self) -> None:
        """Why `multi_file.diff` is kept. Numbering restarts per file, so the same
        number is a valid anchor in several of them at once — 41 line numbers here
        are anchorable in two or more files, and nine in three. A finding is only
        locatable as a (file, line) pair, and a single-file fixture cannot catch a
        reviewer that gets the number right and the file wrong."""
        files = [
            f
            for f in parse_diff(
                (FIXTURES / "diffs" / "multi_file.diff").read_text(encoding="utf-8")
            ).files
            if f.hunks
        ]
        shared = Counter(line for f in files for line in f.anchorable_lines)

        assert len(files) >= 3, "fixture must span several files to be worth keeping"
        assert max(shared.values()) >= 3, "no line number is anchorable in three files"
        assert sum(1 for n in shared.values() if n >= 2) >= 20, (
            "too little overlap to catch a file/line mismatch"
        )

    def test_removed_lines_get_no_number(self) -> None:
        """A removed line has no position in the new file. Numbering it would offer
        the model an anchor that cannot exist."""
        parsed = parse_diff(
            "diff --git a/f.py b/f.py\n"
            "--- a/f.py\n"
            "+++ b/f.py\n"
            "@@ -1,2 +1,2 @@\n"
            " keep\n"
            "-gone\n"
            "+added\n"
        )
        rendered = [ln for ln in parsed.files[0].numbered_text.splitlines() if "gone" in ln]

        assert rendered, "the removed line should still be visible"
        assert rendered[0].startswith(" " * 6), "but it must not carry a number"

    @pytest.mark.parametrize("path", DIFF_FIXTURES, ids=lambda p: p.name)
    def test_numbers_are_ascending_within_a_file(self, path) -> None:
        """Holds across hunk boundaries too — `parser.py` in the multi-file fixture
        has four hunks and the gutter must jump, not restart."""
        for file in parse_diff(path.read_text(encoding="utf-8")).files:
            gutter = self._gutter(file)
            assert gutter == sorted(gutter), file.path


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
            numbered_text="",
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
