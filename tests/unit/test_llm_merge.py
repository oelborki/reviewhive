"""The deterministic half of the merge pass.

The suite cannot tell you whether the prompt works — it is offline by design, so
it pins which pairs get asked about and what happens to the answers, and says
nothing about how the question lands. `scripts/probe_merge.py` measures that, in
one call, against the same labeled fixture used here.

What is worth pinning offline is everything a wrong answer must not be able to
do: merge a pair nobody asked about, merge two findings from the same agent, or
turn a failed call into a failed review.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.stubs import StubAnthropic, overloaded_error

from reviewhive.config import Settings
from reviewhive.graph.llm_merge import (
    MergeDecision,
    MergeDecisions,
    candidate_pairs,
    merge_findings,
)
from reviewhive.models import MergedFinding

FIXTURE = Path(__file__).parent.parent / "fixtures" / "merge_pairs.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def finding(**kwargs) -> MergedFinding:
    base = {
        "file": "app/main.py",
        "line": 44,
        "severity": "high",
        "category": "authz",
        "title": "done endpoint lacks authentication",
        "body": "Nothing checks the caller.",
        "confidence": 0.95,
        "sources": ["architecture"],
    }
    return MergedFinding(**{**base, **kwargs})


@pytest.fixture
def settings() -> Settings:
    return Settings(anthropic_api_key="test")


def pairs(findings, *, window: int = 8, max_pairs: int = 24):
    return candidate_pairs(findings, window=window, max_pairs=max_pairs)


class TestCandidatePairs:
    def test_two_agents_close_together_are_a_candidate(self) -> None:
        found = pairs([finding(), finding(line=46, sources=["security"])])

        assert [(p.left, p.right) for p in found] == [(0, 1)]

    def test_findings_in_different_files_are_never_paired(self) -> None:
        found = pairs([finding(), finding(file="app/other.py", sources=["security"])])

        assert found == []

    def test_a_path_written_two_ways_is_still_one_file(self) -> None:
        """A model echoing back `a/app/main.py` names the same file, and the
        deterministic pass already normalises this."""
        found = pairs([finding(), finding(file="a/app/main.py", sources=["security"])])

        assert len(found) == 1

    def test_findings_beyond_the_window_are_not_paired(self) -> None:
        found = pairs([finding(), finding(line=200, sources=["security"])])

        assert found == []

    def test_two_findings_from_the_same_agent_are_never_paired(self) -> None:
        """The measured hazard. `reports.py:40` and `:44` were the same agent
        reporting two injection sites four lines apart — it had the whole diff in
        front of it when it decided they were two, and asking again is how a real
        vulnerability gets merged away."""
        found = pairs([finding(), finding(line=45)])

        assert found == []

    def test_an_overlapping_source_is_enough_to_exclude(self) -> None:
        """A finding already merged from two agents shares one of them with a
        third. That is still the same agent reporting twice."""
        found = pairs(
            [
                finding(sources=["architecture", "style"]),
                finding(line=45, sources=["style"]),
            ]
        )

        assert found == []

    def test_two_file_level_findings_are_a_candidate(self) -> None:
        found = pairs([finding(line=None), finding(line=None, sources=["security"])])

        assert len(found) == 1

    def test_a_file_level_finding_never_pairs_with_an_anchored_one(self) -> None:
        """With no position to compare, proximity says nothing — the same rule
        the deterministic pass follows."""
        found = pairs([finding(line=None), finding(line=44, sources=["security"])])

        assert found == []

    def test_the_pair_count_is_capped(self) -> None:
        """A cost guard. Pairs grow quadratically with findings on one file."""
        crowd = [
            finding(line=44, sources=[name])
            for name in ("architecture", "security", "style")
        ]
        assert len(pairs(crowd, max_pairs=2)) == 2

    @pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
    def test_the_fixture_agrees_with_candidate_selection(self, case) -> None:
        """Each labeled pair either reaches the model or is filtered here, and the
        fixture says which. That flag is not decoration: a negative case that is
        never asked about proves nothing about the prompt, and scoring it would
        report a mark the prompt did not earn.

        One of the ten is filtered — `mixed/auth.py:17` pairs an
        architecture+style finding with an architecture one, which is the same
        agent reporting twice.
        """
        found = pairs([MergedFinding(**case["left"]), MergedFinding(**case["right"])])

        assert len(found) == (1 if case["reaches_model"] else 0), case["id"]

    def test_the_labeled_set_is_balanced(self) -> None:
        """Five merging and four not. A set of positives alone would tune the pass
        toward merging everything, which is the failure that costs findings."""
        asked = [c for c in CASES if c["reaches_model"]]

        assert sum(c["should_merge"] for c in asked) == 5
        assert sum(not c["should_merge"] for c in asked) == 4


class TestApply:
    async def test_a_pair_judged_the_same_becomes_one_finding(self, settings) -> None:
        findings = [finding(), finding(line=44, sources=["security"], title="no authz check")]
        stub = StubAnthropic(
            outputs={
                "MergeDecisions": MergeDecisions(
                    decisions=[MergeDecision(left=0, right=1, same_defect=True, reason="same")]
                )
            }
        )

        merged, call = await merge_findings(stub, settings, findings)

        assert len(merged) == 1
        assert set(merged[0].sources) == {"architecture", "security"}
        assert call is not None and call.agent == "merge"

    async def test_agreement_does_not_raise_confidence(self, settings) -> None:
        """The invariant that makes absorbing the architecture residue safe. The
        reviewers see the same diff and converge on whatever is salient, so a
        second report is a restatement rather than corroboration."""
        findings = [
            finding(confidence=0.95),
            finding(line=44, sources=["security"], title="no authz check", confidence=0.9),
        ]
        stub = StubAnthropic(
            outputs={
                "MergeDecisions": MergeDecisions(
                    decisions=[MergeDecision(left=0, right=1, same_defect=True, reason="same")]
                )
            }
        )

        merged, _ = await merge_findings(stub, settings, findings)

        assert merged[0].confidence == 0.95

    async def test_a_pair_judged_different_is_left_alone(self, settings) -> None:
        findings = [finding(), finding(line=44, sources=["security"], title="no authz check")]
        stub = StubAnthropic(
            outputs={
                "MergeDecisions": MergeDecisions(
                    decisions=[
                        MergeDecision(left=0, right=1, same_defect=False, reason="different")
                    ]
                )
            }
        )

        merged, _ = await merge_findings(stub, settings, findings)

        assert len(merged) == 2

    async def test_a_decision_about_a_pair_nobody_asked_about_is_dropped(
        self, settings
    ) -> None:
        """An invented index would otherwise merge two arbitrary findings. These
        two are in different files and were never a candidate."""
        findings = [finding(), finding(file="app/other.py", sources=["security"])]
        stub = StubAnthropic(
            outputs={
                "MergeDecisions": MergeDecisions(
                    decisions=[MergeDecision(left=0, right=1, same_defect=True, reason="!")]
                )
            }
        )

        merged, call = await merge_findings(stub, settings, findings)

        assert len(merged) == 2
        # Nothing was a candidate, so no call was made at all.
        assert call is None

    async def test_sameness_is_transitive(self, settings) -> None:
        """If A is B and B is C, all three are one defect — otherwise the result
        depends on which pairs happened to be asked in which order."""
        findings = [
            finding(sources=["architecture"]),
            finding(line=45, sources=["security"], title="no authz check"),
            finding(line=46, sources=["style"], title="unauthenticated handler"),
        ]
        stub = StubAnthropic(
            outputs={
                "MergeDecisions": MergeDecisions(
                    decisions=[
                        MergeDecision(left=0, right=1, same_defect=True, reason="same"),
                        MergeDecision(left=1, right=2, same_defect=True, reason="same"),
                    ]
                )
            }
        )

        merged, _ = await merge_findings(stub, settings, findings)

        assert len(merged) == 1
        assert set(merged[0].sources) == {"architecture", "security", "style"}


class TestDegradation:
    async def test_nothing_to_ask_about_costs_nothing(self, settings) -> None:
        """An empty question is not worth a row, let alone a call."""
        stub = StubAnthropic()

        merged, call = await merge_findings(stub, settings, [finding()])

        assert len(merged) == 1
        assert call is None
        assert stub.parse_calls == []

    async def test_an_api_error_leaves_the_findings_alone(self, settings) -> None:
        """Deduplication is an improvement on a review, never a precondition."""
        findings = [finding(), finding(line=44, sources=["security"], title="other")]
        stub = StubAnthropic(errors={"MergeDecisions": overloaded_error()})

        merged, call = await merge_findings(stub, settings, findings)

        assert len(merged) == 2
        assert "APIStatusError" in call.error

    async def test_unusable_output_leaves_the_findings_alone(self, settings) -> None:
        findings = [finding(), finding(line=44, sources=["security"], title="other")]
        stub = StubAnthropic(outputs={})

        merged, call = await merge_findings(stub, settings, findings)

        assert len(merged) == 2
        assert "unusable" in call.error

    async def test_a_refusal_leaves_the_findings_alone(self, settings) -> None:
        findings = [finding(), finding(line=44, sources=["security"], title="other")]
        stub = StubAnthropic(
            outputs={"MergeDecisions": MergeDecisions(decisions=[])},
            refusals={"MergeDecisions"},
        )

        merged, call = await merge_findings(stub, settings, findings)

        assert len(merged) == 2
        assert call.error is not None

    async def test_truncated_decisions_are_still_applied(self, settings) -> None:
        """The pairs that were judged still merge; the rest go unmerged, which is
        the safe direction."""
        findings = [finding(), finding(line=44, sources=["security"], title="other")]
        stub = StubAnthropic(
            outputs={
                "MergeDecisions": MergeDecisions(
                    decisions=[MergeDecision(left=0, right=1, same_defect=True, reason="same")]
                )
            },
            stop_reasons={"MergeDecisions": "max_tokens"},
        )

        merged, call = await merge_findings(stub, settings, findings)

        assert len(merged) == 1
        assert call.error == "truncated at max_tokens"


class TestPrompt:
    def test_the_boundary_is_stated_before_the_criteria(self) -> None:
        """A prohibition placed after the enumeration does not bind — measured
        three times in this project now, on reviewer prompts and on
        `reconsider.md`. The asymmetry between a missed merge and a wrong one is
        the load-bearing instruction, so it has to come first."""
        from reviewhive.agents.base import load_prompt

        prompt = load_prompt("merge.md", shared=False)

        assert prompt.index("they are different") < prompt.index("## The test")
        assert prompt.index("not judging the findings") < prompt.index("## Same defect")
