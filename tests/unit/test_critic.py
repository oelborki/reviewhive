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

# Long enough that a window around the last line cannot reach the first. The hunk
# header is computed from the body rather than written out: a miscounted header
# makes the parser reject the file, the agents are skipped, and the result is an
# empty review indistinguishable from a clean one.
BODY = [
    "import os",
    "import hmac",
    "",
    'API_HEADER = "x-api-key"',
    "",
    "",
    'def require_api_key(key: str = "") -> None:',
    '    expected = os.environ.get("API_KEY", "")',
    "    if not expected or not hmac.compare_digest(key, expected):",
    '        raise RuntimeError("unauthorized")',
    "",
    "",
    "def _pad_a() -> None:",
    "    return None",
    "",
    "",
    "def _pad_b() -> None:",
    "    return None",
    "",
    "",
    "def _pad_c() -> None:",
    "    return None",
    "",
    "",
    "def _pad_d() -> None:",
    "    return None",
    "",
    "",
    "def check(key: str) -> bool:",
    "    require_api_key(key)",
    "    return True",
]

DIFF = (
    "diff --git a/app/main.py b/app/main.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/app/main.py\n"
    f"@@ -0,0 +1,{len(BODY)} @@\n"
) + "".join(f"+{line}\n" for line in BODY)

FILES = parse_diff(DIFF).files
LAST_LINE = len(BODY)


def finding(**kwargs) -> MergedFinding:
    base = {
        "file": "app/main.py",
        "line": 9,
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
    outcome = await review_findings(stub, settings, findings, files)
    return outcome.findings, outcome.retracted, outcome.call


class TestJudgeable:
    def _judged(self, findings, *, max_findings: int = 30):
        return judgeable(findings, FILES, max_findings=max_findings)

    def test_an_anchored_finding_is_paired_with_its_file(self) -> None:
        judged = self._judged([finding()])

        assert set(judged) == {0}
        assert judged[0].path == "app/main.py"

    def test_a_file_level_finding_is_not_judged(self) -> None:
        """No anchor means no place to read, and a verdict reached with no evidence
        is the guess this pass exists to avoid. It passes through untouched."""
        assert self._judged([finding(line=None)]) == {}

    def test_a_finding_naming_a_file_outside_the_diff_is_not_judged(self) -> None:
        assert self._judged([finding(file="app/other.py")]) == {}

    def test_a_finding_whose_line_is_not_anchorable_is_not_judged(self) -> None:
        assert self._judged([finding(line=900)]) == {}

    def test_the_cap_is_a_prefix_not_a_filter(self) -> None:
        """Findings arrive in the order the agents produced them, so keeping the
        first N leaves a fair sample. Dropping from the middle would not."""
        judged = self._judged([finding(), finding(line=8), finding(line=4)], max_findings=2)

        assert set(judged) == {0, 1}

    def test_a_path_the_model_echoed_back_with_a_prefix_still_matches(self) -> None:
        assert set(self._judged([finding(file="b/app/main.py")])) == {0}


class TestVerdicts:
    async def test_keep_leaves_the_finding_alone(self, settings) -> None:
        stub = StubAnthropic(
            outputs=verdicts(
                CriticVerdict(code_prevents_it=False, index=0, verdict="keep", reason="stands")
            )
        )

        kept, retracted, call = await run(stub, settings, [finding()])

        assert kept == [finding()]
        assert retracted == 0
        assert call is not None and call.agent == "critic"

    async def test_drop_removes_the_finding_and_counts_it(self, settings) -> None:
        stub = StubAnthropic(
            outputs=verdicts(
                CriticVerdict(code_prevents_it=True, index=0, verdict="drop", reason="fails closed")
            )
        )

        kept, retracted, _ = await run(stub, settings, [finding(), finding(line=8)])

        assert [f.line for f in kept] == [8]
        assert retracted == 1

    async def test_amend_lowers_severity_and_records_that_it_did(self, settings) -> None:
        stub = StubAnthropic(
            outputs=verdicts(
                CriticVerdict(
                    code_prevents_it=True,
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
                    code_prevents_it=True,
                    index=0,
                    verdict="amend",
                    revised_severity="high",
                    reason="worse than filed",
                )
            )
        )

        kept, _, _ = await run(stub, settings, [finding(severity="low")])

        assert kept[0].severity == "low"
        assert kept[0].amended is False

    async def test_lowering_a_finding_that_is_not_high_is_refused(self, settings) -> None:
        """Only a high finding can have overstated itself, and overstatement is what
        this pass corrects. Left to itself the model does lower them: given a whole
        file to read it took a correct medium down to low."""
        stub = StubAnthropic(
            outputs=verdicts(
                CriticVerdict(
                    code_prevents_it=True,
                    index=0,
                    verdict="amend",
                    revised_severity="low",
                    reason="milder than filed",
                )
            )
        )

        kept, _, _ = await run(stub, settings, [finding(severity="medium")])

        assert kept[0].severity == "medium"
        assert kept[0].amended is False

    async def test_lowering_without_naming_a_guard_is_refused(self, settings) -> None:
        """A severity comes down because the code stops the thing happening, not
        because the reviewer sounded excited. Given a whole file, the pass began
        lowering a true missing-authorization finding on the grounds that the
        endpoints around it were unguarded too — how widespread a flaw is says
        nothing about whether it is real."""
        stub = StubAnthropic(
            outputs=verdicts(
                CriticVerdict(
                    code_prevents_it=False,
                    index=0,
                    verdict="amend",
                    revised_severity="medium",
                    reason="the endpoints nearby are unguarded too",
                )
            )
        )

        kept, _, _ = await run(stub, settings, [finding(severity="high")])

        assert kept[0].severity == "high"
        assert kept[0].amended is False

    async def test_a_high_finding_can_still_be_lowered(self, settings) -> None:
        stub = StubAnthropic(
            outputs=verdicts(
                CriticVerdict(
                    code_prevents_it=True,
                    index=0,
                    verdict="amend",
                    revised_severity="low",
                    reason="a guard prevents it",
                )
            )
        )

        kept, _, _ = await run(stub, settings, [finding(severity="high")])

        assert kept[0].severity == "low"

    async def test_a_rewritten_title_and_body_are_kept(self, settings) -> None:
        stub = StubAnthropic(
            outputs=verdicts(
                CriticVerdict(
                    code_prevents_it=True,
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
            outputs=verdicts(
                CriticVerdict(code_prevents_it=True, index=0, verdict="amend", reason="no change")
            )
        )

        kept, _, _ = await run(stub, settings, [finding()])

        assert kept[0].amended is False

    async def test_blank_revisions_are_ignored_rather_than_written(self, settings) -> None:
        stub = StubAnthropic(
            outputs=verdicts(
                CriticVerdict(
                    code_prevents_it=True,
                    index=0,
                    verdict="amend",
                    revised_title="   ",
                    revised_body="",
                    reason="x",
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
            outputs=verdicts(
                CriticVerdict(code_prevents_it=True, index=1, verdict="drop", reason="invented")
            )
        )

        # Index 1 is file-level, so it is never judgeable and never asked about.
        kept, retracted, _ = await run(stub, settings, [finding(), finding(line=None)])

        assert len(kept) == 2
        assert retracted == 0

    async def test_the_first_of_two_verdicts_on_one_finding_wins(self, settings) -> None:
        stub = StubAnthropic(
            outputs=verdicts(
                CriticVerdict(code_prevents_it=False, index=0, verdict="keep", reason="first"),
                CriticVerdict(code_prevents_it=True, index=0, verdict="drop", reason="second"),
            )
        )

        kept, retracted, _ = await run(stub, settings, [finding()])

        assert len(kept) == 1
        assert retracted == 0

    async def test_order_is_preserved(self, settings) -> None:
        stub = StubAnthropic(
            outputs=verdicts(
                CriticVerdict(code_prevents_it=True, index=1, verdict="drop", reason="gone"),
            )
        )
        findings = [finding(line=4), finding(line=8), finding(line=9)]

        kept, _, _ = await run(stub, settings, findings)

        assert [f.line for f in kept] == [4, 9]


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
            outputs=verdicts(
                CriticVerdict(code_prevents_it=True, index=0, verdict="drop", reason="refuted")
            ),
            stop_reasons={"CriticVerdicts": "max_tokens"},
        )

        kept, retracted, call = await run(stub, settings, [finding(), finding(line=8)])

        assert retracted == 1
        assert len(kept) == 1
        # Named by file, because one file truncating says nothing about the others.
        assert call is not None and call.error == "app/main.py: truncated at max_tokens"


class TestFieldOrder:
    """Generation is autoregressive, so a field is written with only the fields
    above it in context. `verdict` used to sit second and was committed with nothing
    but an index in front of it, leaving `reason` to justify a choice already made.
    This is the same property `Finding` is ordered for, and it is pinned the same
    way."""

    def test_the_reading_is_written_before_the_verdict(self) -> None:
        order = list(CriticVerdict.model_fields)

        assert order.index("reason") < order.index("verdict")

    def test_the_revisions_follow_the_verdict_that_licenses_them(self) -> None:
        order = list(CriticVerdict.model_fields)

        for field in ("revised_severity", "revised_title", "revised_body"):
            assert order.index("verdict") < order.index(field)

    def test_the_json_schema_preserves_declaration_order(self) -> None:
        """The property is only real if the order survives into what the API is
        sent."""
        properties = list(CriticVerdict.model_json_schema()["properties"])

        assert properties == list(CriticVerdict.model_fields)
        assert properties.index("reason") < properties.index("verdict")


class TestTheQuestionAsked:
    async def test_the_code_is_sent_with_the_finding(self, settings) -> None:
        """Without the code the critic can only agree with whatever the body
        asserts — every false positive measured reads as coherent on its own."""
        stub = StubAnthropic(outputs=verdicts())

        await run(stub, settings, [finding()])

        sent = stub.user_messages[0]
        assert "compare_digest" in sent
        assert "reported by: security" in sent
        assert '<finding index="0">' in sent

    async def test_a_small_file_is_sent_whole_not_as_a_window(self, settings) -> None:
        """The defect this grouping exists for. A claim about one line is often
        settled elsewhere in the file, and a window bounded by a line count cannot
        promise to contain it."""
        stub = StubAnthropic(outputs=verdicts())

        # Anchored at the last line, so a window would not reach the import at the top.
        await run(stub, settings, [finding(line=LAST_LINE)])

        assert "import hmac" in stub.user_messages[0]

    async def test_one_copy_of_the_file_serves_every_finding_on_it(self, settings) -> None:
        """Sending the same file once per finding is what the windows did, and on a
        defect-dense file it costs more than sending it whole."""
        stub = StubAnthropic(outputs=verdicts())

        await run(stub, settings, [finding(line=4), finding(line=9), finding(line=LAST_LINE)])

        sent = stub.user_messages[0]
        assert sent.count("<code>") == 1
        assert sent.count("<finding index=") == 3

    async def test_a_file_too_large_to_send_whole_falls_back_to_windows(self, settings) -> None:
        """The bound is on the file, not on the claim, so the degradation is to the
        behaviour this replaced rather than to something new."""
        stub = StubAnthropic(outputs=verdicts())
        narrow = settings.model_copy(update={"critic_max_file_lines": 1})

        await review_findings(stub, narrow, [finding(line=LAST_LINE)], FILES)

        assert "import hmac" not in stub.user_messages[0]
        assert "compare_digest" in stub.user_messages[0]

    async def test_the_configured_temperature_reaches_the_api(self, settings) -> None:
        stub = StubAnthropic(outputs=verdicts())

        await run(stub, settings, [finding()])

        assert stub.parse_kwargs[0]["temperature"] == settings.agent_temperature

    async def test_unjudgeable_findings_are_not_described_to_the_model(self, settings) -> None:
        stub = StubAnthropic(outputs=verdicts())

        await run(stub, settings, [finding(), finding(line=None, title="file-level remark")])

        assert "file-level remark" not in stub.user_messages[0]
