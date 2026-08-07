"""Narrowing a review to something a reviewer asked about.

The behaviour worth pinning is not that focus reaches the model — it is that a
narrowed run says so. A review that examined part of a diff and reads like a
verdict on all of it is the same failure as silently skipping files.
"""

from __future__ import annotations

import pytest
from tests.stubs import StubAnthropic, finding

from reviewhive.agents.base import _user_message, run_agent
from reviewhive.agents.definitions import SECURITY
from reviewhive.config import Settings
from reviewhive.graph.build import review_diff
from reviewhive.models import AgentCall, MergedFinding, ReviewResult
from reviewhive.render import render_summary

# A string that only needs to reach the model, never the parser.
STUB_DIFF = "diff --git a/app.py b/app.py\n@@ -1 +1 @@\n+x = 1\n"


@pytest.fixture
def settings() -> Settings:
    return Settings(anthropic_api_key="test")


class TestUserMessage:
    def test_an_unfocused_run_says_nothing_about_focus(self) -> None:
        assert "<focus>" not in _user_message(STUB_DIFF)

    def test_the_focus_is_carried_verbatim(self) -> None:
        message = _user_message(STUB_DIFF, "the auth changes")

        assert "<focus>\nthe auth changes\n</focus>" in message

    def test_an_agent_with_nothing_to_say_is_told_to_stay_quiet(self) -> None:
        """The empty-lane failure: an agent whose specialty has no bearing on the
        focus otherwise widens the focus to have something to report. `mixed.diff`
        already showed the same agent answering zero and four on identical input
        when its lane was empty."""
        message = _user_message(STUB_DIFF, "error handling")

        assert "return\nno findings" in message or "return no findings" in message
        assert "do not widen" in message


class TestThroughTheGraph:
    async def test_focus_reaches_every_agent(self, diff_text, settings) -> None:
        client = StubAnthropic(responses={"security": [finding()]})

        await review_diff(diff_text, client, settings, focus="the auth changes")

        reviewed = [
            message
            for call, message in zip(client.parse_calls, client.user_messages, strict=True)
            if call in {"security", "style", "architecture"}
        ]

        assert len(reviewed) == 3
        for prompt in reviewed:
            assert "the auth changes" in prompt

    async def test_the_critic_is_not_given_the_focus(self, diff_text, settings) -> None:
        """Focus narrows what is worth *looking for*, and the critic is not looking
        for anything. It checks a claim someone else made against the lines that
        claim names, and a finding is no less true for falling outside the focus."""
        client = StubAnthropic(responses={"security": [finding()]})

        await review_diff(diff_text, client, settings, focus="the auth changes")

        judged = [
            message
            for call, message in zip(client.parse_calls, client.user_messages, strict=True)
            if call == "CriticVerdicts"
        ]

        assert judged, "the critic should have been asked about the finding"
        assert "the auth changes" not in judged[0]

    async def test_an_unfocused_run_is_unchanged(self, diff_text, settings) -> None:
        client = StubAnthropic(responses={"security": [finding()]})

        result = await review_diff(diff_text, client, settings)

        assert result.focus is None
        for prompt in client.user_messages:
            assert "<focus>" not in prompt

    async def test_the_focus_is_carried_onto_the_result(self, diff_text, settings) -> None:
        client = StubAnthropic(responses={"security": [finding()]})

        result = await review_diff(diff_text, client, settings, focus="error handling")

        assert result.focus == "error handling"

    async def test_run_agent_accepts_focus_directly(self, settings) -> None:
        client = StubAnthropic(responses={"security": [finding()]})

        await run_agent(SECURITY, client, STUB_DIFF, settings, focus="the auth changes")

        assert "the auth changes" in client.user_messages[0]


class TestDisclosure:
    def result(self, **overrides) -> ReviewResult:
        base = {
            "findings": [
                MergedFinding(
                    file="app.py",
                    line=1,
                    severity="high",
                    category="sql-injection",
                    title="Query built by concatenation",
                    body="Parameterise it.",
                    confidence=0.9,
                    sources=["security"],
                )
            ],
            "calls": [
                AgentCall(
                    agent="security",
                    model="claude-haiku-4-5",
                    input_tokens=100,
                    output_tokens=20,
                )
            ],
        }
        return ReviewResult(**{**base, **overrides})

    def test_a_narrowed_review_says_it_was_narrowed(self) -> None:
        summary = render_summary(self.result(focus="the auth changes"))

        assert "Narrowed to: the auth changes" in summary

    def test_an_unnarrowed_review_says_nothing(self) -> None:
        assert "Narrowed to" not in render_summary(self.result())

    def test_a_narrowed_review_with_no_findings_still_discloses(self) -> None:
        """The dangerous case. Without the note this reads as a clean bill of
        health for the entire diff, when only part of it was examined."""
        summary = render_summary(self.result(findings=[], focus="the auth changes"))

        assert "Narrowed to: the auth changes" in summary
        assert "No issues found" in summary
