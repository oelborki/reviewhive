from __future__ import annotations

from reviewhive.anchors import anchor_findings
from reviewhive.diff.parser import DiffFile, DiffHunk
from reviewhive.models import MergedFinding


def diff_file(path: str, added: set[int], context: set[int] | None = None) -> DiffFile:
    hunk = DiffHunk(
        text="",
        added_lines=frozenset(added),
        context_lines=frozenset(context or ()),
        removed_lines=frozenset(),
    )
    return DiffFile(path=path, old_path=None, header="", hunks=(hunk,))


def finding(*, file: str = "src/app/auth.py", line: int | None = 13) -> MergedFinding:
    return MergedFinding(
        file=file,
        line=line,
        severity="high",
        category="sql-injection",
        title="SQL query built by string concatenation",
        body="Use a parameterised query.",
        confidence=0.9,
        sources=["security"],
    )


FILES = [diff_file("src/app/auth.py", added={13, 16, 17}, context={10, 11, 12})]


def test_exact_line_is_kept() -> None:
    report = anchor_findings([finding(line=13)], FILES)

    assert report.findings[0].line == 13
    assert report.snapped == 0
    assert report.unanchored == 0


def test_near_miss_snaps_and_is_counted() -> None:
    report = anchor_findings([finding(line=14)], FILES)

    assert report.findings[0].line in {13, 16}
    assert report.snapped == 1


def test_implausible_line_degrades_to_file_level() -> None:
    """The finding survives; only its location is discarded."""
    report = anchor_findings([finding(line=9000)], FILES)

    assert len(report.findings) == 1
    assert report.findings[0].line is None
    assert report.unanchored == 1


def test_finding_naming_a_file_outside_the_diff_is_dropped() -> None:
    report = anchor_findings([finding(file="src/app/imaginary.py")], FILES)

    assert report.findings == []
    assert report.dropped_files == ["src/app/imaginary.py"]


def test_file_level_finding_passes_through() -> None:
    report = anchor_findings([finding(line=None)], FILES)

    assert len(report.findings) == 1
    assert report.findings[0].line is None
    assert report.unanchored == 0, "a finding with no line was never mis-anchored"


def test_paths_are_normalized_before_lookup() -> None:
    """A model echoing back the diff's `b/` prefix should still resolve."""
    report = anchor_findings([finding(file="b/src/app/auth.py")], FILES)

    assert len(report.findings) == 1
    assert report.findings[0].file == "src/app/auth.py"


def test_no_reviewed_files_drops_everything() -> None:
    report = anchor_findings([finding()], [])

    assert report.findings == []
    assert report.dropped_files == ["src/app/auth.py"]


def test_tolerance_is_configurable() -> None:
    strict = anchor_findings([finding(line=15)], FILES, tolerance=0)
    lenient = anchor_findings([finding(line=15)], FILES, tolerance=5)

    assert strict.findings[0].line is None
    assert lenient.findings[0].line == 16
