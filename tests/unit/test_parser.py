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
    def test_numbered_lines_reassemble_numbered_text(self, path) -> None:
        """The two renderings are one list in `_to_hunk`, and this is what keeps
        them one. Anything slicing a window out of a hunk reads `numbered_lines`;
        if it could drift from the string the model was shown, a finding would be
        judged against lines nobody reviewed."""
        for file in parse_diff(path.read_text(encoding="utf-8")).files:
            for hunk in file.hunks:
                assert "".join(text for _, text in hunk.numbered_lines) == hunk.numbered_text

    @pytest.mark.parametrize("path", DIFF_FIXTURES, ids=lambda p: p.name)
    def test_numbered_lines_carry_the_number_in_their_own_gutter(self, path) -> None:
        """The pairing is the point: a number beside text that does not show it
        would let a slice claim a line it does not contain."""
        for file in parse_diff(path.read_text(encoding="utf-8")).files:
            for number, text in (ln for h in file.hunks for ln in h.numbered_lines):
                if number is None:
                    assert not text[:5].strip().isdigit()
                else:
                    assert int(text[:5]) == number

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


class TestWindow:
    """`window` is what shows a finding the lines it is a claim about. The critic
    pass judges a claim against these lines and nothing else, so a window that
    misrepresents the code produces a confident verdict on something that was never
    written."""

    SPLIT_CONDITION = (
        "diff --git a/f.py b/f.py\n"
        "--- a/f.py\n"
        "+++ b/f.py\n"
        "@@ -1,3 +1,3 @@\n"
        " alpha\n"
        "-bravo\n"
        "+charlie\n"
        " delta\n"
    )

    TWO_HUNKS = (
        "diff --git a/f.py b/f.py\n"
        "--- a/f.py\n"
        "+++ b/f.py\n"
        "@@ -1,1 +1,2 @@\n"
        " one\n"
        "+two\n"
        "@@ -100,1 +101,2 @@\n"
        " hundred\n"
        "+hundred_one\n"
    )

    @staticmethod
    def _file(diff_text: str) -> DiffFile:
        return parse_diff(diff_text).files[0]

    def test_returns_the_lines_around_the_anchor(self) -> None:
        window = self._file(self.TWO_HUNKS).window(2, radius=1)

        assert "one" in window
        assert "two" in window
        assert "hundred" not in window

    def test_keeps_a_removed_line_sitting_inside_the_range(self) -> None:
        """A removed line has no new-file number, so filtering on the number alone
        would drop it. A condition rewritten across a `-`/`+` pair has to read as it
        was written or the reader is judging half of it."""
        window = self._file(self.SPLIT_CONDITION).window(2, radius=1)

        assert "-bravo" in window
        assert "+charlie" in window

    def test_separate_hunks_are_marked_not_glued(self) -> None:
        """Line 2 and line 101 are not adjacent code. Concatenating them invents a
        control flow nobody wrote, which is exactly the mistake a reader of a
        compound condition would then make confidently."""
        window = self._file(self.TWO_HUNKS).window(51, radius=60)

        assert "two" in window
        assert "hundred_one" in window
        assert "[reviewHive: lines omitted]" in window

    def test_a_line_reaching_nothing_returns_empty(self) -> None:
        assert self._file(self.TWO_HUNKS).window(500) == ""

    def test_file_with_no_hunks_returns_empty(self) -> None:
        assert DiffFile(path="f.py", old_path=None, header="").window(1) == ""

    def test_window_lines_are_a_subset_of_the_full_rendering(self) -> None:
        """Whatever the window shows, it is text the reviewers were shown. It never
        re-renders and never re-derives a number."""
        for path in DIFF_FIXTURES:
            for file in parse_diff(path.read_text(encoding="utf-8")).files:
                for line in sorted(file.anchorable_lines)[:5]:
                    for rendered in file.window(line).splitlines():
                        if rendered == "[reviewHive: lines omitted]":
                            continue
                        assert rendered in file.numbered_text, (path.name, file.path, line)
