"""Turning a `ReviewResult` into the text a human reads.

Phase 3 posts `render_summary` as the review body and `render_comment` as each
inline comment. Keeping both here means the CLI and the GitHub path render
identically, so what you see locally is what lands on the PR.

The summary always states what was *not* reviewed. A bot that quietly skipped half
the diff is worse than one that reviewed nothing, because the reader cannot tell.
"""

from __future__ import annotations

from reviewhive.models import MergedFinding, ReviewResult, Severity
from reviewhive.pricing import total_cost

SEVERITY_LABEL: dict[Severity, str] = {
    "high": "High",
    "medium": "Medium",
    "low": "Low",
}

AGENT_LABEL = {
    "security": "Security",
    "style": "Style",
    "architecture": "Architecture",
}


def render_comment(finding: MergedFinding) -> str:
    """The body of one inline review comment."""
    # "independently" would be a claim we cannot support: the reviewers see the
    # same diff and converge on the same salient defects, so a second report is
    # not an independent confirmation. State who raised it and let the reader judge.
    agreement = (
        f" · raised by {len(finding.sources)} reviewers" if len(finding.sources) > 1 else ""
    )
    attribution = ", ".join(AGENT_LABEL.get(s, s) for s in finding.sources)

    return (
        f"**{finding.title}**\n\n"
        f"{finding.body}\n\n"
        f"<sub>{SEVERITY_LABEL[finding.severity]} · `{finding.category}` · "
        f"{attribution}{agreement}</sub>"
    )


def render_summary(result: ReviewResult) -> str:
    """The review body: a verdict, a grouped index, and an honest coverage note."""
    lines: list[str] = ["## reviewHive"]

    if result.focus:
        # Disclosed for the same reason skipped files are: a run that looked at
        # part of the diff must not read as a verdict on all of it.
        lines.append("")
        lines.append(f"_Narrowed to: {result.focus}_")

    if not result.findings:
        lines.append("")
        lines.append(_no_findings_verdict(result))
    else:
        lines.append("")
        lines.append(_headline(result))
        for severity in ("high", "medium", "low"):
            group = [f for f in result.findings if f.severity == severity]
            if not group:
                continue
            lines.append("")
            lines.append(f"### {SEVERITY_LABEL[severity]}")
            lines.extend(_index_entry(f) for f in group)

    if result.suppressed_count:
        lines.append("")
        lines.append(
            f"_{result.suppressed_count} further finding(s) not shown — "
            f"below the reporting thresholds, or beyond the posting cap._"
        )

    if result.retracted_count:
        # Its own line, not added to the suppression count. A suppressed finding is
        # one this review stands behind and had no room for; a retracted one is a
        # claim it checked and withdrew. Saying so is also the only thing that makes
        # an over-eager critic visible on a real pull request — a pass that deletes
        # too much and says nothing looks exactly like a clean diff.
        lines.append("")
        lines.append(
            f"_{result.retracted_count} finding(s) withdrawn on review — "
            f"the lines they named did not support the claim._"
        )

    unplaced = _unplaced_note(result)
    if unplaced:
        lines.append("")
        lines.append(unplaced)

    coverage = _coverage_note(result)
    if coverage:
        lines.append("")
        lines.append(coverage)

    lines.append("")
    lines.append(_footer(result))
    return "\n".join(lines)


def _headline(result: ReviewResult) -> str:
    counts = {
        severity: sum(1 for f in result.findings if f.severity == severity)
        for severity in ("high", "medium", "low")
    }
    parts = [
        f"**{count} {SEVERITY_LABEL[sev].lower()}**" for sev, count in counts.items() if count
    ]
    return f"Found {', '.join(parts)}."


def _no_findings_verdict(result: ReviewResult) -> str:
    """The clean verdict, which must not overstate itself.

    "No issues found" is a claim about the diff. When findings existed and a
    threshold removed them it is a claim about the *configuration*, and saying the
    former would be false — so the qualified wording is load-bearing, not padding.
    The count itself follows in the suppression line.

    A retraction is deliberately *not* qualified the same way. A suppressed finding
    is one this review stands behind and had no room for, so a clean verdict over
    the top of it would overstate. A retracted one is a claim the review checked and
    withdrew, and "no issues found" is then exactly what it means. The count still
    appears on its own line, because a reader has to be able to see the pass act.
    """
    scope = " in the files reviewed" if result.skipped_files else ""
    if result.suppressed_count:
        return f"Nothing found{scope} above the reporting threshold."
    return f"No issues found{scope}."


def _index_entry(finding: MergedFinding) -> str:
    location = f"`{finding.file}`" + (f":{finding.line}" if finding.line else "")
    agreement = f" ({len(finding.sources)} agents)" if len(finding.sources) > 1 else ""
    return f"- {location} — {finding.title}{agreement}"


def _unplaced_note(result: ReviewResult) -> str:
    """Findings with no line, in full.

    These reach no other surface. `anchors.py` clears the line of anything it
    cannot place in the diff, which means no inline comment can carry them, and
    the index above is a title on its own. A claim without its reasoning is worse
    than no claim, so the body belongs here.

    Derived from the finding rather than passed in by the caller: having nowhere
    to anchor is a property of the finding, and `review_local.py --markdown`
    promises to print what would be posted — a parameter the CLI does not pass
    would quietly make that false.
    """
    unplaced = [f for f in result.findings if f.line is None]
    if not unplaced:
        return ""

    entries = "\n\n".join(
        f"**{finding.title}** in `{finding.file}`\n\n{finding.body}" for finding in unplaced
    )
    return (
        f"<details>\n<summary>File-level findings ({len(unplaced)})</summary>\n\n"
        f"{entries}\n\n</details>"
    )


def _coverage_note(result: ReviewResult) -> str:
    """Say plainly what was left out, and why."""
    sections: list[str] = []

    if result.truncated_files:
        shortened = ", ".join(f"`{entry}`" for entry in result.truncated_files)
        sections.append(f"Shortened to fit the review budget: {shortened}")

    if result.skipped_files:
        skipped = ", ".join(f"`{entry}`" for entry in result.skipped_files)
        sections.append(f"Not reviewed: {skipped}")

    if not sections:
        return ""

    body = "\n".join(f"- {section}" for section in sections)
    return f"<details>\n<summary>Coverage</summary>\n\n{body}\n\n</details>"


def _footer(result: ReviewResult) -> str:
    failed = [c.agent for c in result.calls if c.error]
    note = f" · {len(failed)} agent(s) errored: {', '.join(failed)}" if failed else ""
    return (
        f"<sub>3 agents · {total_cost(result):.4f} USD · "
        f"{sum(c.input_tokens + c.output_tokens for c in result.calls):,} tokens{note}</sub>"
    )
