"""Turning a reviewed result into a review-comment payload.

This translates; it does not re-derive. `anchors.py` has already checked every
line against the parsed diff, snapped the near misses, cleared the implausible
ones and dropped findings naming files outside the diff. Re-deciding any of that
here would mean two places that can disagree about which anchors are valid, and
GitHub rejects the *entire* review request when one of them is wrong.

So the whole rule is `line is not None`. Findings that kept a line become inline
comments; the rest are already carried by `render_summary`.
"""

from __future__ import annotations

from dataclasses import dataclass

from reviewhive.models import MergedFinding, ReviewResult
from reviewhive.render import render_comment, render_summary

# Appended when GitHub rejects the anchors and the review is re-posted without
# them. Coverage is always disclosed, and "the comments are missing" is coverage.
DEGRADED_NOTE = (
    "\n\n<sub>GitHub rejected the inline anchors for this review; "
    "every finding is listed above.</sub>"
)


@dataclass(frozen=True)
class ReviewPayload:
    """What `POST /repos/{repo}/pulls/{n}/reviews` needs."""

    body: str
    comments: list[dict[str, object]]
    commit_id: str | None


def to_review_comment(finding: MergedFinding) -> dict[str, object]:
    """One inline comment.

    `side` is stated rather than left to GitHub's default. `anchorable_lines` is
    built from `target_line_no`, so these are new-file numbers; on the LEFT side
    the same number points at different code. Relying on an unstated default for
    the one field that decides whether a comment lands on the right code is a bad
    trade for four characters.

    There is deliberately no `position` key. That legacy field is an offset into
    the hunk rather than a file line, and mixing the two is the classic way to
    put a comment thirty lines from where it belongs.
    """
    return {
        "path": finding.file,
        "line": finding.line,
        "side": "RIGHT",
        "body": render_comment(finding),
    }


def build_review(result: ReviewResult, *, commit_id: str | None) -> ReviewPayload:
    """The review to post for this result.

    `commit_id` should be the head sha the diff was fetched at. Omitting it means
    "latest", and if the pull request advanced between the fetch and the post,
    every anchor silently re-points into a different version of the file.
    """
    inline = [finding for finding in result.findings if finding.line is not None]
    return ReviewPayload(
        body=render_summary(result),
        comments=[to_review_comment(finding) for finding in inline],
        commit_id=commit_id,
    )


def degrade(payload: ReviewPayload) -> ReviewPayload:
    """The same review with nothing to anchor.

    Drops `commit_id` along with the comments. A summary-only review has nothing
    to place, so pinning it to a sha buys nothing — and a stale sha is itself a
    cause of the rejection this is recovering from, so keeping it would risk
    failing the retry for a second reason.
    """
    return ReviewPayload(
        body=payload.body + DEGRADED_NOTE,
        comments=[],
        commit_id=None,
    )
