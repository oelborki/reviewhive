from __future__ import annotations

import pytest

from reviewhive.graph.dedupe import (
    collapse,
    jaccard,
    normalize_path,
    rank_and_cut,
    title_tokens,
)
from reviewhive.models import AgentName, MergedFinding, Severity


def merged(
    *,
    source: AgentName = "security",
    file: str = "src/app/auth.py",
    line: int | None = 10,
    severity: Severity = "medium",
    category: str = "generic",
    title: str = "Something is wrong here",
    confidence: float = 0.8,
) -> MergedFinding:
    return MergedFinding(
        file=file,
        line=line,
        severity=severity,
        category=category,
        title=title,
        body="Body text.",
        confidence=confidence,
        sources=[source],
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("src/app/auth.py", "src/app/auth.py"),
        ("./src/app/auth.py", "src/app/auth.py"),
        ("b/src/app/auth.py", "src/app/auth.py"),
        ("a/src/app/auth.py", "src/app/auth.py"),
        ("src\\app\\auth.py", "src/app/auth.py"),
        ("  src/app/auth.py  ", "src/app/auth.py"),
    ],
)
def test_path_normalization(raw: str, expected: str) -> None:
    assert normalize_path(raw) == expected


def test_grammatical_filler_is_stripped_from_titles() -> None:
    assert title_tokens("The token is in the source") == frozenset({"token", "source"})


def test_domain_nouns_are_kept_as_signal() -> None:
    """`function` and `class` must survive tokenization or they cannot distinguish
    anything. Stripping them would reduce both titles below to {much}."""
    assert title_tokens("Function does too much") == {"function", "does", "too", "much"}
    assert title_tokens("Class does too much") == {"class", "does", "too", "much"}


def test_known_limitation_short_titles_sharing_filler_can_collide() -> None:
    """Documented weakness of token-overlap matching.

    Two four-word titles differing in exactly one word score 0.6 and will merge if
    they are also within the line tolerance in the same file. Co-located and
    three-quarters identical is usually the same issue, so this is an accepted
    trade — but it is a real false-merge risk on terse titles, which is why the
    prompts ask for specific ones.
    """
    function_issue = title_tokens("Function does too much")
    class_issue = title_tokens("Class does too much")

    assert jaccard(function_issue, class_issue) > 0.5


def test_hyphenated_compound_matches_its_unhyphenated_spelling() -> None:
    """Agents disagree on the spelling of the same compound within one review."""
    assert title_tokens("Hard-coded secret") == title_tokens("Hardcoded secret")


def test_a_spaced_dash_still_separates_words() -> None:
    """Only an intra-word hyphen joins. A dash used as punctuation must not glue
    its neighbours into one token."""
    assert title_tokens("Token exposed - move to env") == frozenset(
        {"token", "exposed", "move", "env"}
    )


def test_known_limitation_hyphenated_phrases_stop_matching_their_spaced_form() -> None:
    """The cost of joining hyphen halves, accepted deliberately.

    A hyphen spanning two independent words ("SQL-injection risk") now yields one
    joined token that no longer matches the spaced spelling ("SQL injection
    risk"). That direction produces a missed merge — a duplicate survives — while
    the compound case it fixes was also a missed merge, and is the one observed
    against a live model. Both failures are duplicates rather than false merges,
    so the trade is between two equal-severity misses, resolved in favour of the
    one that actually happens.
    """
    assert jaccard(title_tokens("SQL-injection risk"), title_tokens("SQL injection risk")) < 0.5


def test_jaccard_of_disjoint_titles_is_zero() -> None:
    assert jaccard(title_tokens("SQL injection risk"), title_tokens("Poor naming choice")) == 0.0


class TestCollapse:
    def test_same_issue_from_two_agents_merges(self) -> None:
        result = collapse(
            [
                merged(source="security", line=10, title="SQL query built by concatenation"),
                merged(source="architecture", line=11, title="SQL query built via concatenation"),
            ]
        )

        assert len(result) == 1
        assert result[0].sources == ["security", "architecture"]
        assert result[0].agreement == 2

    def test_hyphenation_alone_does_not_prevent_a_merge(self) -> None:
        """Regression: the two titles below are the ones a live review produced for
        a single hardcoded token. Splitting on the hyphen scored them 0.375 and
        left the duplicate in the posted review."""
        result = collapse(
            [
                merged(source="security", line=47, title="Hardcoded API token in source code"),
                merged(source="style", line=47, title="Hard-coded API token exposed in source"),
            ]
        )

        assert len(result) == 1
        assert result[0].sources == ["security", "style"]

    def test_merge_keeps_highest_severity(self) -> None:
        result = collapse(
            [
                merged(source="style", severity="low", title="Duplicated parsing logic"),
                merged(source="architecture", severity="high", title="Duplicated parsing logic"),
            ]
        )

        assert result[0].severity == "high"

    def test_merge_keeps_the_most_severe_members_wording(self) -> None:
        result = collapse(
            [
                merged(source="style", severity="low", title="Duplicated parsing logic here"),
                merged(
                    source="architecture",
                    severity="high",
                    title="Duplicated parsing logic across modules",
                    category="duplication",
                ),
            ]
        )

        assert result[0].title == "Duplicated parsing logic across modules"
        assert result[0].category == "duplication"

    def test_agreement_does_not_raise_confidence(self) -> None:
        """The agents are not independent observers — single-agent probes show they
        converge on whatever is most salient in the diff, so a second report is the
        same observation restated. Confidence is the most confident member's, full
        stop."""
        result = collapse(
            [
                merged(source="security", confidence=0.9, title="Duplicated parsing logic"),
                merged(source="style", confidence=0.5, title="Duplicated parsing logic"),
                merged(source="architecture", confidence=0.5, title="Duplicated parsing logic"),
            ]
        )

        assert result[0].agreement == 3
        assert result[0].confidence == 0.9

    def test_a_reviewer_outside_its_lane_cannot_promote_the_finding_it_duplicates(self) -> None:
        """The failure this guards against. The architecture reviewer files a
        security finding at high confidence; it merges with security's own copy.
        Under the old bonus that scope violation pushed the finding's confidence up
        and its rank with it — a duplicate masquerading as corroboration."""
        solo = merged(source="security", confidence=0.95, title="Token hardcoded in source")
        poached = collapse(
            [
                merged(source="security", confidence=0.95, title="Token hardcoded in source"),
                merged(source="architecture", confidence=0.95, title="Token hardcoded in source"),
            ]
        )[0]

        assert poached.confidence == solo.confidence

    def test_merged_line_is_the_earliest_reported(self) -> None:
        result = collapse(
            [
                merged(line=12, title="Duplicated parsing logic"),
                merged(source="style", line=10, title="Duplicated parsing logic"),
            ]
        )

        assert result[0].line == 10

    def test_distant_lines_do_not_merge(self) -> None:
        result = collapse(
            [
                merged(line=10, title="Duplicated parsing logic"),
                merged(source="style", line=200, title="Duplicated parsing logic"),
            ],
            line_tolerance=3,
        )

        assert len(result) == 2

    def test_different_files_do_not_merge(self) -> None:
        result = collapse(
            [
                merged(file="a.py", title="Duplicated parsing logic"),
                merged(file="b.py", source="style", title="Duplicated parsing logic"),
            ]
        )

        assert len(result) == 2

    def test_same_location_different_issues_do_not_merge(self) -> None:
        result = collapse(
            [
                merged(line=10, title="SQL query built by concatenation"),
                merged(source="style", line=10, title="Function name does not describe behaviour"),
            ]
        )

        assert len(result) == 2

    def test_file_level_findings_only_merge_with_file_level(self) -> None:
        result = collapse(
            [
                merged(line=None, title="Module lacks error handling"),
                merged(source="style", line=10, title="Module lacks error handling"),
            ]
        )

        assert len(result) == 2, "with no position to compare, proximity says nothing"

    def test_two_file_level_findings_do_merge(self) -> None:
        result = collapse(
            [
                merged(line=None, title="Module lacks error handling"),
                merged(source="style", line=None, title="Module lacks any error handling"),
            ]
        )

        assert len(result) == 1

    def test_paths_are_normalized_before_comparison(self) -> None:
        result = collapse(
            [
                merged(file="src/a.py", title="Duplicated parsing logic"),
                merged(file="b/src/a.py", source="style", title="Duplicated parsing logic"),
            ]
        )

        assert len(result) == 1
        assert result[0].file == "src/a.py"

    def test_a_single_finding_passes_through_untouched(self) -> None:
        original = merged()
        assert collapse([original]) == [original]

    def test_empty_input(self) -> None:
        assert collapse([]) == []

    def test_same_agent_reporting_twice_does_not_inflate_agreement(self) -> None:
        result = collapse(
            [
                merged(source="security", line=10, title="Duplicated parsing logic"),
                merged(source="security", line=11, title="Duplicated parsing logic"),
            ]
        )

        assert result[0].sources == ["security"]
        assert result[0].agreement == 1


class TestRankAndCut:
    def test_orders_by_severity_then_confidence_then_agreement(self) -> None:
        """A second reviewer is a tiebreaker, not a trump card. Two agents at 0.6
        do not outrank one agent at 0.9, because the second report is usually the
        same observation restated rather than a second opinion."""
        low = merged(severity="low", title="Low one", file="c.py")
        high_solo = merged(severity="high", title="High solo", file="b.py", confidence=0.9)
        high_agreed = merged(severity="high", title="High agreed", file="a.py", confidence=0.6)
        high_agreed = high_agreed.model_copy(update={"sources": ["security", "style"]})

        ranked, _ = rank_and_cut([low, high_solo, high_agreed], min_confidence=0.0, max_posted=10)

        assert [f.title for f in ranked] == ["High solo", "High agreed", "Low one"]

    def test_agreement_breaks_ties_between_equally_confident_findings(self) -> None:
        solo = merged(severity="high", title="Solo", file="b.py", confidence=0.8)
        agreed = merged(severity="high", title="Agreed", file="c.py", confidence=0.8)
        agreed = agreed.model_copy(update={"sources": ["security", "style"]})

        ranked, _ = rank_and_cut([solo, agreed], min_confidence=0.0, max_posted=10)

        assert [f.title for f in ranked] == ["Agreed", "Solo"]

    def test_filters_below_confidence_floor(self) -> None:
        ranked, suppressed = rank_and_cut(
            [merged(confidence=0.9, title="Keep"), merged(confidence=0.1, title="Drop")],
            min_confidence=0.5,
            max_posted=10,
        )

        assert [f.title for f in ranked] == ["Keep"]
        assert suppressed == 1, "a finding removed by the floor is still withheld from the reader"

    def test_the_floor_and_the_cap_are_counted_together(self) -> None:
        """One number reaches the reader, so it has to cover both reasons a finding
        is missing. Counting only the cap made a fully-filtered review render as a
        clean one."""
        findings = [
            merged(title="A", file="a.py", confidence=0.9),
            merged(title="B", file="b.py", confidence=0.9),
            merged(title="C", file="c.py", confidence=0.1),
        ]

        ranked, suppressed = rank_and_cut(findings, min_confidence=0.5, max_posted=1)

        assert len(ranked) == 1
        assert suppressed == 2, "one below the floor, one past the cap"

    def test_cap_reports_the_remainder(self) -> None:
        findings = [merged(title=f"Issue {i}", line=i * 100) for i in range(10)]
        ranked, suppressed = rank_and_cut(findings, min_confidence=0.0, max_posted=3)

        assert len(ranked) == 3
        assert suppressed == 7

    def test_ordering_is_stable_for_identical_ranks(self) -> None:
        findings = [
            merged(title="Same", file="z.py", line=1),
            merged(title="Same", file="a.py", line=1),
        ]
        ranked, _ = rank_and_cut(findings, min_confidence=0.0, max_posted=10)

        assert [f.file for f in ranked] == ["a.py", "z.py"]
