"""Answering a question about a review.

Whether the answer is any good is a prompt question, measured by
`scripts/probe_mention.py`. What is testable here is the context the model is
given, and that a failure produces no reply rather than a broken one.
"""

from __future__ import annotations

import pytest
from tests.stubs import StubAnthropic, overloaded_error

from reviewhive.config import Settings
from reviewhive.mentions.intent import PriorFinding
from reviewhive.mentions.respond import Answer, Verdict, answer_question, reconsider

DIFF = "diff --git a/app/db.py b/app/db.py\n@@ -1 +1 @@\n+q = 'SELECT ' + owner\n"
FINDINGS = [
    PriorFinding(0, "app/db.py", 56, "high", "SQL injection in search_tasks"),
    PriorFinding(1, "app/auth.py", 5, "high", "API secret hardcoded"),
]


@pytest.fixture
def settings() -> Settings:
    return Settings(anthropic_api_key="test")


def stub(reply: str | None = "Because the value is concatenated.", **kwargs) -> StubAnthropic:
    return StubAnthropic(
        outputs={"Answer": Answer(reply=reply)} if reply is not None else {}, **kwargs
    )


class TestContext:
    async def test_the_question_and_diff_are_both_supplied(self, settings) -> None:
        client = stub()

        await answer_question(
            client,
            settings,
            question="why is this a problem?",
            diff_text=DIFF,
            findings=FINDINGS,
        )

        message = client.user_messages[0]
        assert "why is this a problem?" in message
        assert "SELECT" in message

    async def test_prior_findings_are_listed(self, settings) -> None:
        client = stub()

        await answer_question(
            client, settings, question="why?", diff_text=DIFF, findings=FINDINGS
        )

        assert "SQL injection in search_tasks" in client.user_messages[0]

    async def test_a_targeted_question_names_its_finding_separately(self, settings) -> None:
        """A reply inside one comment thread is about that finding. Leaving the
        model to pick it out of a list invites an answer about a different one."""
        client = stub()

        await answer_question(
            client,
            settings,
            question="why?",
            diff_text=DIFF,
            findings=FINDINGS,
            target=FINDINGS[1],
        )

        message = client.user_messages[0]
        assert "The question concerns this finding" in message
        assert message.index("concerns this finding") < message.index("Findings already posted")

    async def test_an_untargeted_question_names_no_finding(self, settings) -> None:
        client = stub()

        await answer_question(
            client, settings, question="why?", diff_text=DIFF, findings=FINDINGS
        )

        assert "concerns this finding" not in client.user_messages[0]


class TestOutput:
    async def test_the_reply_is_returned_with_its_call(self, settings) -> None:
        client = stub("Because `owner` is concatenated into the query.")

        reply, call = await answer_question(
            client, settings, question="why?", diff_text=DIFF, findings=FINDINGS
        )

        assert reply == "Because `owner` is concatenated into the query."
        assert call.agent == "answer"
        assert call.input_tokens > 0

    async def test_the_schema_cannot_carry_a_finding(self) -> None:
        """The structural half of the boundary the prompt states in words: there
        is no severity, file or line for a new defect to occupy, so "while I was
        here" has nowhere to go."""
        assert set(Answer.model_fields) == {"reply"}


class TestReconsider:
    def verdict_stub(self, verdict: Verdict | None = None, **kwargs) -> StubAnthropic:
        return StubAnthropic(
            outputs={"Verdict": verdict} if verdict else {}, **kwargs
        )

    async def test_the_original_reasoning_is_supplied(self, settings) -> None:
        """Without it the model defends a one-line title whose argument it cannot
        see, which makes caving the path of least resistance."""
        client = self.verdict_stub(Verdict(stands=True, reply="It still stands."))

        await reconsider(
            client,
            settings,
            rebuttal="this is intentional",
            finding=FINDINGS[0],
            body="`owner` is interpolated into the query without escaping.",
            diff_text=DIFF,
        )

        message = client.user_messages[0]
        assert "`owner` is interpolated into the query without escaping." in message
        assert "this is intentional" in message

    async def test_a_sustained_finding_is_machine_readable(self, settings) -> None:
        """`stands` is separate from the prose so "did pushback retire this?" is a
        query rather than an exercise in reading tone."""
        client = self.verdict_stub(Verdict(stands=True, reply="Still exploitable."))

        verdict, call = await reconsider(
            client,
            settings,
            rebuttal="it's fine",
            finding=FINDINGS[0],
            body="reasoning",
            diff_text=DIFF,
        )

        assert verdict is not None and verdict.stands is True
        assert call.agent == "reconsider"

    async def test_a_withdrawal_is_machine_readable(self, settings) -> None:
        client = self.verdict_stub(Verdict(stands=False, reply="You're right, withdrawing."))

        verdict, _ = await reconsider(
            client,
            settings,
            rebuttal="validated in middleware",
            finding=FINDINGS[0],
            body="reasoning",
            diff_text=DIFF,
        )

        assert verdict is not None and verdict.stands is False

    async def test_severity_can_drop_without_withdrawing(self, settings) -> None:
        """Real but less important is a distinct outcome from wrong, and the
        schema has to be able to say so."""
        client = self.verdict_stub(
            Verdict(stands=True, revised_severity="low", reply="Fair, lowering it.")
        )

        verdict, _ = await reconsider(
            client,
            settings,
            rebuttal="only reachable from an admin path",
            finding=FINDINGS[0],
            body="reasoning",
            diff_text=DIFF,
        )

        assert verdict is not None
        assert verdict.stands is True
        assert verdict.revised_severity == "low"

    async def test_a_failure_yields_no_verdict(self, settings) -> None:
        """Silence is right here. A finding whose challenge could not be judged
        must not be silently retired."""
        client = self.verdict_stub(errors={"Verdict": overloaded_error()})

        verdict, call = await reconsider(
            client,
            settings,
            rebuttal="anything",
            finding=FINDINGS[0],
            body="reasoning",
            diff_text=DIFF,
        )

        assert verdict is None
        assert call.error


class TestFailure:
    async def test_an_api_error_yields_no_reply(self, settings) -> None:
        client = stub(errors={"Answer": overloaded_error()})

        reply, call = await answer_question(
            client, settings, question="why?", diff_text=DIFF, findings=FINDINGS
        )

        assert reply is None
        assert call.error

    async def test_a_refusal_yields_no_reply(self, settings) -> None:
        client = stub(refusals={"Answer"})

        reply, call = await answer_question(
            client, settings, question="why?", diff_text=DIFF, findings=FINDINGS
        )

        assert reply is None
        assert call.error

    async def test_unusable_output_yields_no_reply(self, settings) -> None:
        client = stub(reply=None)

        reply, call = await answer_question(
            client, settings, question="why?", diff_text=DIFF, findings=FINDINGS
        )

        assert reply is None
        assert call.error

    async def test_it_never_raises(self, settings) -> None:
        client = stub(errors={"Answer": overloaded_error()})

        reply, _ = await answer_question(
            client, settings, question="why?", diff_text=DIFF, findings=FINDINGS
        )

        assert reply is None
