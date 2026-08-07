"""The deterministic half of the critic pass.

The suite cannot tell you whether the prompt works — it is offline by design, so it
pins which findings get asked about and what happens to the answers, and says
nothing about how the question lands. `scripts/probe_critic.py` measures that
against the labeled fixture.

What is worth pinning here is everything a wrong answer must not be able to do:
delete a finding nobody asked about, raise a severity, judge a claim with no lines
in front of it, or turn a failed call into a failed review.
"""

from __future__ import annotations

import pytest
from tests.stubs import StubAnthropic, overloaded_error

from reviewhive.config import Settings
from reviewhive.diff.parser import parse_diff
from reviewhive.graph.critic import (
    CriticVerdict,
    CriticVerdicts,
    judgeable,
    review_findings,
)
from reviewhive.models import MergedFinding

DIFF = (
    "diff --git a/app/main.py b/app/main.py\n"
    "--- a/app/main.py\n"
    "+++ b/app/main.py\n"
    "@@ -1,2 +1,6 @@\n"
    " import os\n"
    " import hmac\n"
    "+\n"
    "+def require_api_key(key: str = \"\") -> None:\n"
    "+    expected = os.environ.get(\"API_KEY\", \"\")\n"
    "+    if not expected or not hmac.compare_digest(key, expected):\n"
    "+        raise RuntimeError(\"unauthorized\")\n"
)

FILES = parse_diff(DIFF).files


def finding(**kwargs) -> MergedFinding:
    base = {
        "file": "app/main.py",
        "line": 6,
        "severity": "high",
        "category": "authentication-bypass",
        "title": "empty default allows API key bypass",
        "body": "An unset API_KEY makes expected empty, so compare_digest passes.",
        "confidence": 0.95,
        "sources": ["security"],
    }
    return MergedFinding(**{**base, **kwargs})


@pytest.fixture
def settings() -> Settings:
    return Settings(anthropic_api_key="test")


def verdicts(*items: CriticVerdict) -> dict:
    return {"CriticVerdicts": CriticVerdicts(verdicts=list(items))}


async def run(stub, settings, findings, files=FILES):
    return await review_findings(stub, settings, findings, files)


class TestJudgeable:
    def _windows(self, findings, *, radius: int = 10, max_findings: int = 30):
        return judgeable(findings, FILES, radius=radius, max_findings=max_findings)

    def test_an_anchored_finding_is_shown_its_own_lines(self) -> None:
        windows = self._windows([finding()])

        assert set(windows) == {0}
        assert "compare_digest" in windows[0]

    def test_a_file_level_finding_is_not_judged(self) -> None:
        """No anchor means no window, and a verdict reached with no evidence is the
        guess this pass exists to avoid. It passes through untouched instead."""
        assert self._windows([finding(line=None)]) == {}

    def test_a_finding_naming_a_file_outside_the_diff_is_not_judged(self) -> None:
        assert self._windows([finding(file="app/other.py")]) == {}

    def test_a_finding_whose_line_reaches_no_hunk_is_not_judged(self) -> None:
        assert self._windows([finding(line=900)]) == {}

    def test_the_cap_is_a_prefix_not_a_filter(self) -> None:
        """Findings arrive in the order the agents produced them, so keeping the
        first N leaves a fair sample. Dropping from the middle would not."""
        windows = self._windows([finding(), finding(line=5), finding(line=4)], max_findings=2)

        assert set(windows) == {0, 1}

    def test_a_path_the_model_echoed_back_with_a_prefix_still_matches(self) -> None:
        assert set(self._windows([finding(file="b/app/main.py")])) == {0}


class TestVerdicts:
    async def test_keep_leaves_the_finding_alone(self, settings) -> None:
        stub = StubAnthropic(
            outputs=verdicts(CriticVerdict(index=0, verdict="keep", reason="stands"))
        )

        kept, retracted, call = await run(stub, settings, [finding()])

        assert kept == [finding()]
        assert retracted == 0
        assert call is not None and call.agent == "critic"

    async def test_drop_removes_the_finding_and_counts_it(self, settings) -> None:
        stub = StubAnthropic(
            outputs=verdicts(CriticVerdict(index=0, verdict="drop", reason="fails closed"))
        )

        kept, retracted, _ = await run(stub, settings, [finding(), finding(line=5)])

        assert [f.line for f in kept] == [5]
        assert retracted == 1

    async def test_amend_lowers_severity_and_records_that_it_did(self, settings) -> None:
        stub = StubAnthropic(
            outputs=verdicts(
                CriticVerdict(
                    index=0,
                    verdict="amend",
                    revised_severity="low",
                    reason="the whitelist is right there",
                )
            )
        )

        kept, retracted, _ = await run(stub, settings, [finding()])

        assert kept[0].severity == "low"
        assert kept[0].amended is True
        assert retracted == 0

    async def test_a_severity_increase_is_refused(self, settings) -> None:
        """The defect this pass exists to fix is a finding rated above what its
        evidence supports. Something that can inflate severity can commit it."""
        stub = StubAnthropic(
            outputs=verdicts(
                CriticVerdict(
                    index=0, verdict="amend", revised_severity="high", reason="worse than filed"
                )
            )
        )

        kept, _, _ = await run(stub, settings, [finding(severity="low")])

        assert kept[0].severity == "low"
        assert kept[0].amended is False

    async def test_a_rewritten_title_and_body_are_kept(self, settings) -> None:
        stub = StubAnthropic(
            outputs=verdicts(
                CriticVerdict(
                    index=0,
                    verdict="amend",
                    revised_title="API key falls back to an empty string",
                    revised_body="The check rejects the request; the fallback is still worth "
                    "removing so a misconfiguration fails at startup.",
                    reason="claim overstated",
                )
            )
        )

        kept, _, _ = await run(stub, settings, [finding()])

        assert kept[0].title == "API key falls back to an empty string"
        assert kept[0].body.startswith("The check rejects")
        assert kept[0].amended is True

    async def test_an_amendment_that_changes_nothing_is_a_keep(self, settings) -> None:
        stub = StubAnthropic(
            outputs=verdicts(CriticVerdict(index=0, verdict="amend", reason="no change"))
        )

        kept, _, _ = await run(stub, settings, [finding()])

        assert kept[0].amended is False

    async def test_blank_revisions_are_ignored_rather_than_written(self, settings) -> None:
        stub = StubAnthropic(
            outputs=verdicts(
                CriticVerdict(
                    index=0, verdict="amend", revised_title="   ", revised_body="", reason="x"
                )
            )
        )

        kept, _, _ = await run(stub, settings, [finding()])

        assert kept[0].title == finding().title
        assert kept[0].amended is False


class TestRefusedVerdicts:
    async def test_a_verdict_on_a_finding_nobody_asked_about_is_dropped(self, settings) -> None:
        """The index most likely to be invented is one that was never shown, and
        honouring it would delete an arbitrary finding."""
        stub = StubAnthropic(
            outputs=verdicts(CriticVerdict(index=1, verdict="drop", reason="invented"))
        )

        # Index 1 is file-level, so it is never judgeable and never asked about.
        kept, retracted, _ = await run(stub, settings, [finding(), finding(line=None)])

        assert len(kept) == 2
        assert retracted == 0

    async def test_the_first_of_two_verdicts_on_one_finding_wins(self, settings) -> None:
        stub = StubAnthropic(
            outputs=verdicts(
                CriticVerdict(index=0, verdict="keep", reason="first"),
                CriticVerdict(index=0, verdict="drop", reason="second"),
            )
        )

        kept, retracted, _ = await run(stub, settings, [finding()])

        assert len(kept) == 1
        assert retracted == 0

    async def test_order_is_preserved(self, settings) -> None:
        stub = StubAnthropic(
            outputs=verdicts(
                CriticVerdict(index=1, verdict="drop", reason="gone"),
            )
        )
        findings = [finding(line=4), finding(line=5), finding(line=6)]

        kept, _, _ = await run(stub, settings, findings)

        assert [f.line for f in kept] == [4, 6]


class TestFailureIsNotAReviewFailure:
    async def test_nothing_judgeable_costs_no_call(self, settings) -> None:
        stub = StubAnthropic()

        kept, retracted, call = await run(stub, settings, [finding(line=None)])

        assert call is None
        assert stub.parse_calls == []
        assert kept == [finding(line=None)]
        assert retracted == 0

    async def test_an_api_error_leaves_the_findings_alone(self, settings) -> None:
        stub = StubAnthropic(errors={"CriticVerdicts": overloaded_error()})

        kept, retracted, call = await run(stub, settings, [finding()])

        assert kept == [finding()]
        assert retracted == 0
        assert call is not None and "APIStatusError" in (call.error or "")

    async def test_a_refusal_leaves_the_findings_alone(self, settings) -> None:
        stub = StubAnthropic(refusals={"CriticVerdicts"})

        kept, _, call = await run(stub, settings, [finding()])

        assert kept == [finding()]
        assert call is not None and "refusal" in (call.error or "")

    async def test_unparsed_output_leaves_the_findings_alone(self, settings) -> None:
        stub = StubAnthropic(outputs={})

        kept, _, call = await run(stub, settings, [finding()])

        assert kept == [finding()]
        assert call is not None and call.error is not None

    async def test_truncation_keeps_the_verdicts_that_arrived(self, settings) -> None:
        """A truncated list is still a valid list about the findings it covers. The
        rest go unjudged, which is the safe direction."""
        stub = StubAnthropic(
            outputs=verdicts(CriticVerdict(index=0, verdict="drop", reason="refuted")),
            stop_reasons={"CriticVerdicts": "max_tokens"},
        )

        kept, retracted, call = await run(stub, settings, [finding(), finding(line=5)])

        assert retracted == 1
        assert len(kept) == 1
        assert call is not None and call.error == "truncated at max_tokens"


class TestTheQuestionAsked:
    async def test_the_lines_are_sent_with_the_finding(self, settings) -> None:
        """Without the window the critic can only agree with whatever the body
        asserts — every false positive measured reads as coherent on its own."""
        stub = StubAnthropic(outputs=verdicts())

        await run(stub, settings, [finding()])

        sent = stub.user_messages[0]
        assert "compare_digest" in sent
        assert "reported by: security" in sent
        assert '<finding index="0">' in sent

    async def test_the_configured_temperature_reaches_the_api(self, settings) -> None:
        stub = StubAnthropic(outputs=verdicts())

        await run(stub, settings, [finding()])

        assert stub.parse_kwargs[0]["temperature"] == settings.agent_temperature

    async def test_unjudgeable_findings_are_not_described_to_the_model(self, settings) -> None:
        stub = StubAnthropic(outputs=verdicts())

        await run(stub, settings, [finding(), finding(line=None, title="file-level remark")])

        assert "file-level remark" not in stub.user_messages[0]
