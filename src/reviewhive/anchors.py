"""Reconciling model-reported locations with real diff positions.

Models get the file right far more often than the line. This module is where a
reported location becomes a trusted one, before anything downstream depends on it.
`github/positions.py` translates the result into a review-comment payload; it does
not re-derive it, because an anchor GitHub rejects fails the *entire* review
request rather than the single comment.

Three outcomes per finding:

- **exact / snapped** — the line is real, or close enough to a real one to be an
  off-by-one after a hunk boundary. Keep it as an inline anchor.
- **unanchored** — the file is under review but the line is not credible. Keep the
  finding, drop the line; it degrades into the summary.
- **dropped** — the file is not in the diff at all. The finding is discarded.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from reviewhive.diff.parser import DiffFile
from reviewhive.graph.dedupe import normalize_path
from reviewhive.models import MergedFinding

# How far a reported line may be from a real one before we stop believing it.
# Two or three lines is a plausible miscount after a hunk header; twenty is a guess.
SNAP_TOLERANCE = 5


@dataclass
class AnchorReport:
    findings: list[MergedFinding] = field(default_factory=list)
    snapped: int = 0
    unanchored: int = 0
    dropped_files: list[str] = field(default_factory=list)


def anchor_findings(
    findings: list[MergedFinding],
    files: list[DiffFile],
    *,
    tolerance: int = SNAP_TOLERANCE,
) -> AnchorReport:
    """Validate every finding's location against the diff it was derived from."""
    by_path = {normalize_path(f.path): f for f in files}
    report = AnchorReport()

    for finding in findings:
        path = normalize_path(finding.file)
        diff_file = by_path.get(path)

        if diff_file is None:
            # The model named a file that is not in this diff. Nothing downstream
            # can place it and the reader cannot verify it, so it goes.
            report.dropped_files.append(finding.file)
            continue

        if finding.line is None:
            report.findings.append(finding.model_copy(update={"file": path}))
            continue

        anchor = diff_file.nearest_anchor(finding.line, tolerance=tolerance)
        if anchor is None:
            report.unanchored += 1
        elif anchor != finding.line:
            report.snapped += 1

        report.findings.append(finding.model_copy(update={"file": path, "line": anchor}))

    return report
