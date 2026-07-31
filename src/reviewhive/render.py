"""Turning a `ReviewResult` into the text a human reads.

Phase 3 posts `render_summary` as the review body and `render_comment` as each
inline comment. Keeping both here means the CLI and the GitHub path render
identically, so what you see locally is what lands on the PR.

The summary always states what was *not* reviewed. A bot that quietly skipped half
the diff is worse than one that reviewed nothing, because the reader cannot tell.
"""

from __future__ import annotations

from reviewhive.models import MergedFinding, ReviewResult, Severity

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
    agreement = (
        f" · flagged independently by {len(finding.sources)} reviewers"
        if len(finding.sources) > 1
        else ""
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
            f"only the highest-ranked are posted._"
        )

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
    if not result.skipped_files:
        return "No issues found."
    return "No issues found in the files reviewed."


def _index_entry(finding: MergedFinding) -> str:
    location = f"`{finding.file}`" + (f":{finding.line}" if finding.line else "")
    agreement = f" ({len(finding.sources)} agents)" if len(finding.sources) > 1 else ""
    return f"- {location} — {finding.title}{agreement}"


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
        f"<sub>3 agents · {result.total_cost_usd:.4f} USD · "
        f"{sum(c.input_tokens + c.output_tokens for c in result.calls):,} tokens{note}</sub>"
    )
