"""Translating anchored findings into GitHub review comments.

No `importorskip`: this reaches only `render` and `models`, so it runs on a bare
install. httpx and FastAPI are the endpoint's problem, not this module's.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reviewhive.anchors import anchor_findings
from reviewhive.diff.parser import parse_diff
from reviewhive.github.positions import build_review, degrade, to_review_comment
from reviewhive.models import AgentCall, MergedFinding, ReviewResult

FIXTURES = Path(__file__).parent.parent / "fixtures" / "diffs"


def merged(**overrides) -> MergedFinding:
    base = {
        "file": "src/app/auth.py",
        "line": 13,
        "severity": "high",
        "category": "sql-injection",
        "title": "Query built by concatenation",
        "body": "Use a parameterised query.",
        "confidence": 0.9,
        "sources": ["security"],
    }
    return MergedFinding(**{**base, **overrides})


def result(findings: list[MergedFinding]) -> ReviewResult:
    return ReviewResult(
        findings=findings,
        calls=[
            AgentCall(
                agent="security", model="claude-haiku-4-5", input_tokens=100, output_tokens=20
            )
        ],
    )


class TestComment:
    def test_carries_exactly_the_four_fields_github_needs(self) -> None:
        assert to_review_comment(merged()) == {
            "path": "src/app/auth.py",
            "line": 13,
            "side": "RIGHT",
            "body": to_review_comment(merged())["body"],
        }

    def test_side_is_right_and_stated(self) -> None:
        """`anchorable_lines` holds new-file numbers. On LEFT the same number
        addresses different code, and GitHub's default is not something to rely on
        for the field that decides which line the comment lands on."""
        assert to_review_comment(merged())["side"] == "RIGHT"

    def test_never_emits_the_legacy_position_field(self) -> None:
        """`position` is an offset into the hunk rather than a file line. Sending
        both is how a comment ends up tens of lines from the code it describes."""
        assert "position" not in to_review_comment(merged())


class TestBuildReview:
    def test_a_finding_without_a_line_produces_no_comment(self) -> None:
        payload = build_review(result([merged(line=None)]), commit_id="abc123")

        assert payload.comments == []

    def test_an_unanchored_finding_still_reaches_the_body(self) -> None:
        """It degrades into the summary rather than being dropped."""
        payload = build_review(result([merged(line=None)]), commit_id="abc123")

        assert "Use a parameterised query." in payload.body

    def test_anchored_and_unanchored_findings_split_between_the_two_surfaces(self) -> None:
        payload = build_review(
            result([merged(line=13), merged(line=None, title="Module mixes concerns")]),
            commit_id="abc123",
        )

        assert len(payload.comments) == 1
        assert payload.comments[0]["line"] == 13
        assert "Module mixes concerns" in payload.body

    def test_commit_id_is_carried_through(self) -> None:
        """Omitting it means 'latest'. If the pull request advanced between the
        diff fetch and the post, every anchor re-points into different code."""
        assert build_review(result([merged()]), commit_id="deadbeef").commit_id == "deadbeef"


class TestAgainstARealDiff:
    """`multi_file.diff` is PR #13 as GitHub serves it, and the only fixture whose
    line numbers overlap across files — which is what makes it able to catch a
    right-number-wrong-file anchor."""

    @pytest.fixture
    def files(self):
        return parse_diff((FIXTURES / "multi_file.diff").read_text(encoding="utf-8")).files

    def test_every_emitted_anchor_is_real_in_the_file_it_names(self, files) -> None:
        by_path = {f.path: f for f in files}

        # One finding per file at a line that exists in it, plus two that name a
        # line belonging to a *different* file in the same diff.
        findings = [
            merged(file=path, line=sorted(f.anchorable_lines)[0]) for path, f in by_path.items()
        ]
        findings.append(merged(file="tests/unit/test_anchors.py", line=250))
        findings.append(merged(file="src/reviewhive/diff/budget.py", line=30))

        report = anchor_findings(findings, files)
        payload = build_review(result(report.findings), commit_id="head")

        assert payload.comments, "expected at least one inline comment"
        for comment in payload.comments:
            diff_file = by_path[comment["path"]]
            assert comment["line"] in diff_file.anchorable_lines, (
                f"{comment['path']}:{comment['line']} is not a line of that file"
            )

    def test_a_line_valid_only_in_another_file_does_not_become_a_comment(self, files) -> None:
        """Line 250 exists in parser.py and nowhere near test_anchors.py, which
        spans 8..14. Anchoring must judge it against the file it names."""
        report = anchor_findings([merged(file="tests/unit/test_anchors.py", line=250)], files)
        payload = build_review(result(report.findings), commit_id="head")

        assert payload.comments == []

    def test_a_finding_naming_a_file_outside_the_diff_is_gone(self, files) -> None:
        report = anchor_findings([merged(file="src/not/in/the/diff.py", line=10)], files)
        payload = build_review(result(report.findings), commit_id="head")

        assert payload.comments == []
        assert not report.findings


class TestDegrade:
    def test_drops_every_comment(self) -> None:
        payload = build_review(result([merged(line=13)]), commit_id="abc123")

        assert degrade(payload).comments == []

    def test_drops_the_commit_id_too(self) -> None:
        """A summary-only review has nothing to place, and a stale sha is itself a
        cause of the rejection being recovered from."""
        payload = build_review(result([merged()]), commit_id="abc123")

        assert degrade(payload).commit_id is None

    def test_says_that_the_anchors_were_rejected(self) -> None:
        """Coverage is always disclosed, and comments that silently vanished are
        coverage."""
        degraded = degrade(build_review(result([merged()]), commit_id="abc123"))

        assert "rejected the inline anchors" in degraded.body

    def test_keeps_every_finding_visible_in_the_body(self) -> None:
        payload = build_review(result([merged(title="Hardcoded credential")]), commit_id="x")

        assert "Hardcoded credential" in degrade(payload).body
