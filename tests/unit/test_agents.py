"""How one agent call degrades.

`run_agent` never raises, and that promise is what keeps two working reviewers
producing a review when the third does not. `test_graph.py` covers the exception
path through the whole graph; these cover the three ways a call can come back
*successfully* and still be unusable, which are easy to miss precisely because
nothing throws.

Every case asserts the error is recorded on the `AgentCall` as well as the
findings being empty. A silent zero-finding agent is indistinguishable from a
clean lane, and the summary would report a review nobody performed.
"""

from __future__ import annotations

import pytest
from tests.stubs import StubAnthropic, finding, overloaded_error

from reviewhive.agents.base import PROMPT_DIR, load_prompt, run_agent
from reviewhive.agents.definitions import AGENTS
from reviewhive.config import Settings

SPEC = next(spec for spec in AGENTS if spec.name == "security")
DIFF = "  1 + password = 'hunter2'\n"


@pytest.fixture
def settings() -> Settings:
    return Settings(anthropic_api_key="test")


async def test_a_normal_call_returns_findings_and_telemetry(settings) -> None:
    stub = StubAnthropic(responses={"security": [finding()]})

    outcome = await run_agent(SPEC, stub, DIFF, settings)

    assert len(outcome.findings) == 1
    assert outcome.call.error is None
    assert outcome.call.findings_returned == 1
    assert outcome.call.input_tokens > 0


async def test_a_refusal_is_recorded_rather_than_raised(settings) -> None:
    """The model declined the request. There is nothing to report and nothing
    to retry, but the review must still say the lane was not covered."""
    stub = StubAnthropic(responses={"security": [finding()]}, refusals={"security"})

    outcome = await run_agent(SPEC, stub, DIFF, settings)

    assert outcome.findings == []
    assert outcome.call.error == "refusal"


async def test_unparsed_output_is_recorded_rather_than_raised(settings) -> None:
    """Structured output that did not validate. Distinct from a refusal: the
    model answered, and what came back cannot be used."""
    stub = StubAnthropic(responses={"security": [finding()]}, unparsed={"security"})

    outcome = await run_agent(SPEC, stub, DIFF, settings)

    assert outcome.findings == []
    assert outcome.call.error is not None
    assert "unparsed" in outcome.call.error


async def test_truncated_output_keeps_its_findings_and_says_so(settings) -> None:
    """The subtle one. Hitting the token cap still produces valid structured
    output, so the findings are real and worth keeping — but the list is short
    for a reason that is not "the code is fine", and only the recorded error
    distinguishes the two."""
    stub = StubAnthropic(
        responses={"security": [finding(), finding(line=14)]},
        stop_reasons={"security": "max_tokens"},
    )

    outcome = await run_agent(SPEC, stub, DIFF, settings)

    assert len(outcome.findings) == 2
    assert outcome.call.error == "truncated at max_tokens"


async def test_an_api_error_is_recorded_rather_than_raised(settings) -> None:
    stub = StubAnthropic(errors={"security": overloaded_error()})

    outcome = await run_agent(SPEC, stub, DIFF, settings)

    assert outcome.findings == []
    assert "APIStatusError" in outcome.call.error


async def test_a_failed_call_still_reports_how_long_it_took(settings) -> None:
    """Latency is measured around the call, not inside the success path — a
    review that spent thirty seconds failing should say so."""
    stub = StubAnthropic(errors={"security": overloaded_error()})

    outcome = await run_agent(SPEC, stub, DIFF, settings)

    assert outcome.call.latency_ms >= 0
    assert outcome.call.model == settings.agent_model


class TestPrompts:
    def test_a_reviewer_prompt_carries_the_shared_contract(self) -> None:
        composed = load_prompt(SPEC.prompt_file)
        specialty = load_prompt(SPEC.prompt_file, shared=False)

        assert len(composed) > len(specialty)
        assert composed.endswith(specialty)

    def test_a_non_reviewer_prompt_is_loaded_alone(self) -> None:
        """`_shared.md` is the *reviewer* contract — stay in your lane, read the
        gutter, the finding schema. Prepending it to a classifier would hand it
        instructions for a job it is not doing."""
        alone = load_prompt("intent.md", shared=False)
        preamble = load_prompt("_shared.md", shared=False)

        assert preamble not in alone
        assert alone == (PROMPT_DIR / "intent.md").read_text(encoding="utf-8")

    def test_every_registered_agent_has_a_loadable_prompt(self) -> None:
        """A fourth agent is a prompt file plus one line in the registry. This is
        the tripwire for the half that gets forgotten."""
        for spec in AGENTS:
            assert load_prompt(spec.prompt_file).strip()

    def test_an_unknown_prompt_file_fails_loudly(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_prompt("no_such_prompt.md", shared=False)
