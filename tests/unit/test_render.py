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

    def test_a_filtered_review_is_not_reported_as_a_clean_one(self) -> None:
        """Findings existed and a threshold removed them, so "No issues found" would
        be a false statement about the code rather than a true one about the
        configuration."""
        summary = render_summary(result(findings=[], suppressed_count=4))

        assert "No issues found." not in summary
        assert "Nothing found above the reporting threshold." in summary
        assert "4 further finding(s) not shown" in summary

    def test_a_retraction_is_disclosed(self) -> None:
        """The only thing that makes an over-eager critic visible on a real pull
        request. A pass that deletes too much and says nothing looks exactly like a
        diff nobody had anything to say about."""
        summary = render_summary(result(retracted_count=2))

        assert "2 finding(s) withdrawn on review" in summary

    def test_a_retraction_is_counted_apart_from_suppression(self) -> None:
        """Two different claims. A suppressed finding is one this review stands
        behind and had no room for; a retracted one is a claim it checked and
        withdrew. One total could not tell a reader which had happened."""
        summary = render_summary(result(suppressed_count=3, retracted_count=2))

        assert "3 further finding(s) not shown" in summary
        assert "2 finding(s) withdrawn on review" in summary

    def test_a_retracted_review_may_still_call_itself_clean(self) -> None:
        """Unlike suppression. A threshold withholds findings the review stands
        behind, so a clean verdict over the top of it overstates; a retraction is a
        claim the review withdrew, and "no issues found" is then what it means."""
        summary = render_summary(result(findings=[], retracted_count=3))

        assert "No issues found." in summary
        assert "3 finding(s) withdrawn on review" in summary

    def test_a_filtered_review_with_skips_qualifies_on_both_counts(self) -> None:
        summary = render_summary(
            result(findings=[], suppressed_count=2, skipped_files=["huge.py (binary)"])
        )

        assert "Nothing found in the files reviewed above the reporting threshold." in summary

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

    def test_a_finding_with_no_line_still_states_its_reasoning(self) -> None:
        """The gap this closes: with no line there is no inline comment to carry
        the body, and the index above is a title alone. The reader would get an
        accusation with no argument behind it."""
        summary = render_summary(result(findings=[merged(line=None)]))

        assert "Use a parameterised query instead." in summary
        assert "File-level findings (1)" in summary

    def test_anchored_findings_are_left_to_their_inline_comments(self) -> None:
        """Their bodies belong on the line, not repeated in the summary."""
        summary = render_summary(result(findings=[merged(line=13)]))

        assert "File-level findings" not in summary
        assert "Use a parameterised query instead." not in summary

    def test_only_the_unplaced_findings_are_expanded(self) -> None:
        summary = render_summary(
            result(
                findings=[
                    merged(line=13, body="Anchored body."),
                    merged(line=None, title="Module mixes concerns", body="Unplaced body."),
                ]
            )
        )

        assert "File-level findings (1)" in summary
        assert "Unplaced body." in summary
        assert "Anchored body." not in summary
