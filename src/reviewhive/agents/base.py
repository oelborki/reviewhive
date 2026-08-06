"""The shared call path every agent uses.

Two deliberate choices here:

1. **Structured output via `messages.parse`.** Each agent returns an `AgentFindings`
   instance validated against the schema — no JSON-from-markdown scraping and no
   regex repair path.
2. **An agent failure is not a review failure.** `run_agent` never raises. A dead
   agent returns zero findings and an `AgentCall` carrying the error, so two
   working agents still produce a review. Partial output beats a 500.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from anthropic import APIError, AsyncAnthropic

from reviewhive.config import Settings
from reviewhive.models import AgentCall, AgentFindings, AgentName, Finding

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).parent / "prompts"


@dataclass(frozen=True)
class AgentSpec:
    """Everything that distinguishes one agent from another."""

    name: AgentName
    display: str
    prompt_file: str


@dataclass
class AgentOutcome:
    findings: list[Finding]
    call: AgentCall


@cache
def load_prompt(prompt_file: str, *, shared: bool = True) -> str:
    """Compose a system prompt from the shared preamble plus a specialty.

    Cached because the graph runs per-PR and these never change at runtime.

    `shared=False` loads the file on its own, for prompts that are not reviewers.
    `_shared.md` is the *reviewer* contract — stay in your lane, read the gutter,
    severity and confidence, the finding schema — and none of it applies to a
    classifier or a responder. Prepending it would hand them instructions for a
    job they are not doing.
    """
    specialty = (PROMPT_DIR / prompt_file).read_text(encoding="utf-8")
    if not shared:
        return specialty
    preamble = (PROMPT_DIR / "_shared.md").read_text(encoding="utf-8")
    return f"{preamble}\n\n---\n\n{specialty}"


def make_token_counter(client: AsyncAnthropic, model: str):
    """Adapt the Anthropic client to the `TokenCounter` shape the budget expects."""

    async def count(text: str) -> int:
        result = await client.messages.count_tokens(
            model=model,
            messages=[{"role": "user", "content": text}],
        )
        return result.input_tokens

    return count


async def run_agent(
    spec: AgentSpec,
    client: AsyncAnthropic,
    diff_text: str,
    settings: Settings,
    *,
    focus: str | None = None,
) -> AgentOutcome:
    """Run one agent over the diff. Never raises.

    `focus` narrows the run to something a reviewer asked about. It goes in the
    user turn rather than the system prompt: the system prompt is the agent's
    contract and is identical on every run, which is what makes it cacheable and
    what `load_prompt` memoises. Focus is per-request task context.
    """
    started = time.perf_counter()
    call = AgentCall(agent=spec.name, model=settings.agent_model, input_tokens=0, output_tokens=0)

    try:
        message = await client.messages.parse(
            model=settings.agent_model,
            max_tokens=settings.agent_max_tokens,
            temperature=settings.agent_temperature,
            system=load_prompt(spec.prompt_file),
            messages=[{"role": "user", "content": _user_message(diff_text, focus)}],
            output_format=AgentFindings,
        )
    except APIError as exc:
        # The SDK already retried 429s and 5xx per `max_retries`. Reaching here
        # means this agent is done; the other two carry the review.
        logger.warning("agent %s failed: %s", spec.name, exc)
        call.error = f"{type(exc).__name__}: {exc}"
        call.latency_ms = _elapsed_ms(started)
        return AgentOutcome(findings=[], call=call)

    call.latency_ms = _elapsed_ms(started)
    call.input_tokens = message.usage.input_tokens
    call.output_tokens = message.usage.output_tokens
    call.cache_read_tokens = message.usage.cache_read_input_tokens or 0

    if message.stop_reason == "refusal":
        call.error = "refusal"
        logger.warning("agent %s refused the diff", spec.name)
        return AgentOutcome(findings=[], call=call)

    parsed = message.parsed_output
    if parsed is None:
        call.error = f"unparsed output (stop_reason={message.stop_reason})"
        logger.warning("agent %s returned unparsed output", spec.name)
        return AgentOutcome(findings=[], call=call)

    if message.stop_reason == "max_tokens":
        # Structured output that hit the cap still parses, but is truncated. Keep
        # what came back and record why the list may look short.
        call.error = "truncated at max_tokens"
        logger.warning("agent %s hit max_tokens", spec.name)

    call.findings_returned = len(parsed.findings)
    return AgentOutcome(findings=parsed.findings, call=call)


def _user_message(diff_text: str, focus: str | None = None) -> str:
    instruction = "Review the following pull request diff."
    if focus:
        instruction = (
            "A reviewer has asked for a narrower second look at this pull request:\n\n"
            f"<focus>\n{focus}\n</focus>\n\n"
            "Report only findings that fall inside that focus *and* inside your own "
            "specialty. If your specialty has nothing to say about the focus, return "
            "no findings — do not widen the focus to have something to report."
        )
    return f"{instruction}\n\n<diff>\n{diff_text}\n</diff>"


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
