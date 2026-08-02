from __future__ import annotations

from reviewhive.models import AgentCall, MergedFinding, ReviewResult
from reviewhive.render import render_comment, render_summary


def merged(**overrides) -> MergedFinding:
    base = {
        "file": "src/app/auth.py",
        "line": 13,
        "severity": "high",
        "category": "sql-injection",
        "title": "SQL query built by string concatenation",
        "body": "Use a parameterised query instead.",
        "confidence": 0.9,
        "sources": ["security"],
    }
    return MergedFinding(**{**base, **overrides})


def result(**overrides) -> ReviewResult:
    base = {
        "findings": [merged()],
        "calls": [
            AgentCall(
                agent="security",
                model="claude-haiku-4-5",
                input_tokens=1000,
                output_tokens=200,
            )
        ],
    }
    return ReviewResult(**{**base, **overrides})


class TestComment:
    def test_includes_title_body_and_attribution(self) -> None:
        body = render_comment(merged())

        assert "SQL query built by string concatenation" in body
        assert "Use a parameterised query instead." in body
        assert "Security" in body
        assert "sql-injection" in body

    def test_agreement_is_called_out_when_agents_concur(self) -> None:
        body = render_comment(merged(sources=["security", "architecture"]))

        assert "2 reviewers" in body
        assert "Security" in body
        assert "Architecture" in body

    def test_no_agreement_note_for_a_single_source(self) -> None:
        assert "reviewers" not in render_comment(merged())


class TestSummary:
    def test_groups_findings_by_severity(self) -> None:
        summary = render_summary(
            result(findings=[merged(), merged(severity="low", title="Unclear name", line=47)])
        )

        assert "### High" in summary
        assert "### Low" in summary
        assert summary.index("### High") < summary.index("### Low")

    def test_reports_what_was_not_reviewed(self) -> None:
        summary = render_summary(
            result(
                skipped_files=["package-lock.json (lockfile)"],
                truncated_files=["big.py (900 lines omitted)"],
            )
        )

        assert "package-lock.json (lockfile)" in summary
        assert "big.py (900 lines omitted)" in summary

    def test_states_the_cap_rather_than_hiding_findings(self) -> None:
        assert "3 further finding(s) not shown" in render_summary(result(suppressed_count=3))

    def test_clean_diff_says_so(self) -> None:
        assert "No issues found." in render_summary(result(findings=[]))

    def test_clean_diff_with_skips_qualifies_the_verdict(self) -> None:
        """A clean bill of health must not imply coverage the bot did not have."""
        summary = render_summary(result(findings=[], skipped_files=["huge.py (binary)"]))

        assert "No issues found in the files reviewed." in summary

    def test_footer_reports_cost_and_tokens(self) -> None:
        summary = render_summary(result())

        # 1000 in @ $1/MTok + 200 out @ $5/MTok = $0.002
        assert "0.0020 USD" in summary
        assert "1,200 tokens" in summary

    def test_an_unpriced_model_still_renders_a_footer(self) -> None:
        """Cost telemetry must never be able to take down the review body. An
        unknown model contributes nothing to the total rather than raising, and the
        token count — which does not depend on the price table — stays honest."""
        summary = render_summary(
            result(
                calls=[
                    AgentCall(
                        agent="security",
                        model="claude-imaginary-9",
                        input_tokens=1000,
                        output_tokens=200,
                    )
                ]
            )
        )

        assert "0.0000 USD" in summary
        assert "1,200 tokens" in summary

    def test_failed_agents_are_disclosed(self) -> None:
        summary = render_summary(
            result(
                calls=[
                    AgentCall(
                        agent="security",
                        model="claude-haiku-4-5",
                        input_tokens=0,
                        output_tokens=0,
                        error="APIStatusError: overloaded",
                    )
                ]
            )
        )

        assert "1 agent(s) errored: security" in summary

    def test_file_level_finding_renders_without_a_line(self) -> None:
        summary = render_summary(result(findings=[merged(line=None)]))

        assert "`src/app/auth.py` —" in summary
        assert "auth.py`:" not in summary
