"""Replying to a reviewer without reviewing anything.

The answer path is deliberately not a review. It sees the same diff and the
findings already posted, and its output is prose for a comment thread rather than
a `Finding` — there is no schema here that *could* carry a new defect, which is
the structural half of the boundary the prompt states in words.

Like `run_agent`, nothing here raises. A failed reply is worth recording and worth
saying nothing about on the pull request; it is not worth taking down the handler.
"""

from __future__ import annotations

import logging
import time

from anthropic import APIError, AsyncAnthropic
from pydantic import BaseModel, Field

from reviewhive.agents.base import load_prompt
from reviewhive.config import Settings
from reviewhive.mentions.intent import PriorFinding
from reviewhive.models import AgentCall, Severity

logger = logging.getLogger(__name__)

ANSWER_PROMPT = "answer.md"
RECONSIDER_PROMPT = "reconsider.md"


class Answer(BaseModel):
    """A reply to one question.

    Structured for one reason: the model returns exactly a reply and nothing that
    could be mistaken for a finding. There is no severity, no file, no line to
    fill in, so "while I was here" has nowhere to go.
    """

    reply: str = Field(description="The answer, as markdown for a comment thread.")


class Verdict(BaseModel):
    """What became of a finding that was challenged.

    `stands` is separate from `reply` so the outcome is machine-readable. Whether a
    reviewer's pushback actually retires findings is a question about the whole
    system, and reading it out of prose afterwards would be guesswork.
    """

    stands: bool = Field(description="True if the finding survives the rebuttal.")
    revised_severity: Severity | None = Field(
        default=None,
        description=(
            "New severity when the argument changes how much it matters, "
            "not whether it is real."
        ),
    )
    reply: str = Field(description="What to say to the reviewer, as markdown.")


async def reconsider(
    client: AsyncAnthropic,
    settings: Settings,
    *,
    rebuttal: str,
    finding: PriorFinding,
    body: str,
    diff_text: str,
) -> tuple[Verdict | None, AgentCall]:
    """Re-judge one finding against a reviewer's objection. Never raises.

    `body` is the finding's original reasoning. Without it the model would be
    defending a one-line title it cannot see the argument for, which makes caving
    the path of least resistance.
    """
    started = time.perf_counter()
    call = AgentCall(
        agent="reconsider", model=settings.agent_model, input_tokens=0, output_tokens=0
    )

    try:
        message = await client.messages.parse(
            model=settings.agent_model,
            max_tokens=settings.agent_max_tokens,
            system=load_prompt(RECONSIDER_PROMPT, shared=False),
            messages=[
                {
                    "role": "user",
                    "content": _rebuttal_message(rebuttal, finding, body, diff_text),
                }
            ],
            output_format=Verdict,
        )
    except APIError as exc:
        logger.warning("could not reconsider a finding: %s", exc)
        call.error = f"{type(exc).__name__}: {exc}"
        call.latency_ms = _elapsed_ms(started)
        return None, call

    call.latency_ms = _elapsed_ms(started)
    call.input_tokens = message.usage.input_tokens
    call.output_tokens = message.usage.output_tokens
    call.cache_read_tokens = message.usage.cache_read_input_tokens or 0

    parsed = message.parsed_output
    if parsed is None or message.stop_reason == "refusal":
        call.error = f"unusable output (stop_reason={message.stop_reason})"
        logger.warning("reconsider returned nothing usable")
        return None, call

    logger.info(
        "finding %d %s after rebuttal", finding.ordinal, "stands" if parsed.stands else "withdrawn"
    )
    return parsed, call


def _rebuttal_message(
    rebuttal: str, finding: PriorFinding, body: str, diff_text: str
) -> str:
    return (
        f"The finding under challenge:\n<finding>\n{finding.render()}\n\n{body}\n</finding>\n\n"
        f"<diff>\n{diff_text}\n</diff>\n\n"
        f"What the reviewer said:\n<rebuttal>\n{rebuttal}\n</rebuttal>"
    )


async def answer_question(
    client: AsyncAnthropic,
    settings: Settings,
    *,
    question: str,
    diff_text: str,
    findings: list[PriorFinding],
    target: PriorFinding | None = None,
) -> tuple[str | None, AgentCall]:
    """Answer one question about a reviewed pull request. Never raises."""
    started = time.perf_counter()
    call = AgentCall(
        agent="answer", model=settings.agent_model, input_tokens=0, output_tokens=0
    )

    try:
        message = await client.messages.parse(
            model=settings.agent_model,
            max_tokens=settings.agent_max_tokens,
            system=load_prompt(ANSWER_PROMPT, shared=False),
            messages=[
                {
                    "role": "user",
                    "content": _user_message(question, diff_text, findings, target),
                }
            ],
            output_format=Answer,
        )
    except APIError as exc:
        logger.warning("could not answer a question: %s", exc)
        call.error = f"{type(exc).__name__}: {exc}"
        call.latency_ms = _elapsed_ms(started)
        return None, call

    call.latency_ms = _elapsed_ms(started)
    call.input_tokens = message.usage.input_tokens
    call.output_tokens = message.usage.output_tokens
    call.cache_read_tokens = message.usage.cache_read_input_tokens or 0

    parsed = message.parsed_output
    if parsed is None or message.stop_reason == "refusal":
        call.error = f"unusable output (stop_reason={message.stop_reason})"
        logger.warning("answer returned nothing usable")
        return None, call

    return parsed.reply, call


def _user_message(
    question: str,
    diff_text: str,
    findings: list[PriorFinding],
    target: PriorFinding | None,
) -> str:
    parts = []

    if target is not None:
        # Stated separately from the list so the subject is unambiguous. A reader
        # replying inside one comment thread is asking about that finding, and
        # leaving the model to infer it from a list invites it to answer about a
        # different one.
        parts.append(
            "The question concerns this finding:\n"
            f"<finding>\n{target.render()}\n</finding>"
        )

    if findings:
        listed = "\n".join(f.render() for f in findings)
        parts.append(f"Findings already posted:\n<findings>\n{listed}\n</findings>")

    parts.append(f"<diff>\n{diff_text}\n</diff>")
    parts.append(f"<question>\n{question}\n</question>")
    return "\n\n".join(parts)


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
