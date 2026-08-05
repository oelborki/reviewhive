"""Reading what a mention is asking for.

The classifier's own accuracy is not testable offline — that is a prompt question,
answered by probing against real comments. What *is* testable is everything around
it: when it is not called at all, what happens when it fails, and which source wins
when the payload and the model disagree.
"""

from __future__ import annotations

import pytest
from tests.stubs import StubAnthropic, overloaded_error

from reviewhive.config import Settings
from reviewhive.mentions.intent import (
    CommentIntent,
    PriorFinding,
    classify,
    is_bare_mention,
    strip_mention,
)

HANDLE = "/reviewhive"


@pytest.fixture
def settings() -> Settings:
    return Settings(anthropic_api_key="test")


def intent(**overrides) -> CommentIntent:
    base = {"action": "answer_question", "question": "why?", "rationale": "reads as a question"}
    return CommentIntent(**{**base, **overrides})


def stub(result: CommentIntent | None = None, **kwargs) -> StubAnthropic:
    return StubAnthropic(outputs={"CommentIntent": result} if result else {}, **kwargs)


class TestBareMention:
    @pytest.mark.parametrize(
        "body",
        ["/reviewhive", "  /reviewhive  ", "/reviewhive!", "/reviewhive ?", "/ReviewHive"],
    )
    def test_a_mention_with_no_instruction_is_bare(self, body: str) -> None:
        """Punctuation is not an instruction, and the handle is case-insensitive
        because people type it however they type it."""
        assert is_bare_mention(body, HANDLE)

    @pytest.mark.parametrize("body", ["/reviewhive why?", "/reviewhive re-run", "hey /reviewhive"])
    def test_a_mention_with_words_is_not_bare(self, body: str) -> None:
        assert not is_bare_mention(body, HANDLE)

    def test_the_handle_is_removed_from_the_body(self) -> None:
        assert strip_mention("/reviewhive check the auth changes", HANDLE) == (
            "check the auth changes"
        )

    async def test_a_bare_mention_costs_nothing(self, settings) -> None:
        """The whole reason for the short-circuit: there is nothing to interpret,
        so there is no reason to pay to interpret it."""
        client = stub()

        result, call = await classify(client, settings, comment="/reviewhive")

        assert result.action == "full_review"
        assert call is None
        assert client.parse_calls == []


class TestClassification:
    async def test_a_comment_with_text_is_classified(self, settings) -> None:
        client = stub(intent(action="focused_review", focus="error handling"))

        result, call = await classify(
            client, settings, comment="/reviewhive look again at error handling"
        )

        assert result.action == "focused_review"
        assert result.focus == "error handling"
        assert call is not None
        assert call.agent == "intent"

    async def test_the_handle_is_stripped_before_the_model_sees_it(self, settings) -> None:
        client = stub(intent())

        await classify(client, settings, comment="/reviewhive why is this a problem?")

        assert "/reviewhive" not in client.user_messages[0]
        assert "why is this a problem?" in client.user_messages[0]

    async def test_prior_findings_are_offered_with_their_ordinals(self, settings) -> None:
        """Without ordinals the model cannot name a finding, so `reconsider` could
        never identify its target."""
        client = stub(intent())

        await classify(
            client,
            settings,
            comment="/reviewhive the second one is wrong",
            findings=[
                PriorFinding(0, "app/db.py", 56, "high", "SQL injection"),
                PriorFinding(1, "app/auth.py", 5, "high", "Hardcoded secret"),
            ],
        )

        message = client.user_messages[0]
        assert "[0] high · app/db.py:56 — SQL injection" in message
        assert "[1] high · app/auth.py:5 — Hardcoded secret" in message

    async def test_an_empty_findings_list_says_so(self, settings) -> None:
        client = stub(intent())

        await classify(client, settings, comment="/reviewhive why?", findings=[])

        assert "No findings have been posted" in client.user_messages[0]


class TestThreadTarget:
    async def test_the_payload_beats_the_model(self, settings) -> None:
        """GitHub says which comment a reply answers. A guess must not override a
        fact — this is the one place the two sources can disagree."""
        client = stub(intent(action="reconsider", target_ordinal=7))

        result, _ = await classify(
            client, settings, comment="/reviewhive this is intentional", thread_target=2
        )

        assert result.target_ordinal == 2

    async def test_the_model_is_told_not_to_guess(self, settings) -> None:
        client = stub(intent(action="reconsider"))

        await classify(client, settings, comment="/reviewhive disagree", thread_target=3)

        assert "already determined" in client.user_messages[0]

    async def test_without_a_thread_the_models_answer_stands(self, settings) -> None:
        client = stub(intent(action="reconsider", target_ordinal=4))

        result, _ = await classify(client, settings, comment="/reviewhive the auth one is wrong")

        assert result.target_ordinal == 4


class TestFailure:
    async def test_an_api_error_falls_back_to_answering(self, settings) -> None:
        """Not to a re-review. Losing the ability to read a comment is not a reason
        to spend three agent calls on it, and answering is the cheapest action that
        cannot be wrong in an expensive direction."""
        client = stub(errors={"CommentIntent": overloaded_error()})

        result, call = await classify(client, settings, comment="/reviewhive what about this?")

        assert result.action == "answer_question"
        assert result.question == "what about this?"
        assert call is not None and call.error

    async def test_a_refusal_falls_back_the_same_way(self, settings) -> None:
        client = stub(intent(), refusals={"CommentIntent"})

        result, call = await classify(client, settings, comment="/reviewhive explain")

        assert result.action == "answer_question"
        assert call is not None and call.error

    async def test_unusable_output_falls_back_the_same_way(self, settings) -> None:
        client = stub()  # no canned CommentIntent -> parsed_output is None

        result, call = await classify(client, settings, comment="/reviewhive explain")

        assert result.action == "answer_question"
        assert call is not None and call.error

    async def test_classify_never_raises(self, settings) -> None:
        client = stub(errors={"CommentIntent": overloaded_error()})

        result, _ = await classify(client, settings, comment="/reviewhive anything")

        assert result.action in {
            "full_review",
            "focused_review",
            "answer_question",
            "reconsider",
        }
