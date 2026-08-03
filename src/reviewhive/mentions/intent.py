"""Deciding what a mention wants.

One cheap structured call, then deterministic dispatch. The reading stays natural
language; the routing does not, so a misread shows up as a logged rationale rather
than as behaviour buried inside a free-form reply.

Two things never reach the model:

- **An empty mention.** `@reviewhive` with nothing after it is a full re-review by
  definition, so it short-circuits with no call at all. There is nothing to
  interpret and no reason to pay to interpret it.
- **The finding a threaded reply concerns.** GitHub already says which comment was
  replied to, so that is resolved from the payload. The classifier is only asked to
  guess when the payload does not know.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Literal

from anthropic import APIError, AsyncAnthropic
from pydantic import BaseModel, Field

from reviewhive.agents.base import load_prompt
from reviewhive.config import Settings
from reviewhive.models import AgentCall

logger = logging.getLogger(__name__)

PROMPT_FILE = "intent.md"

MentionAction = Literal["full_review", "focused_review", "answer_question", "reconsider"]


class CommentIntent(BaseModel):
    """What the comment is asking for."""

    action: MentionAction
    focus: str | None = Field(
        default=None, description="Subject to narrow to; only for focused_review."
    )
    target_ordinal: int | None = Field(
        default=None, description="Ordinal of the finding being disputed, if identifiable."
    )
    question: str | None = Field(
        default=None, description="What is being asked; only for answer_question."
    )
    rationale: str = Field(description="How the comment was read. Not the answer to it.")


@dataclass(frozen=True)
class PriorFinding:
    """One finding the bot already posted, as the classifier is shown it."""

    ordinal: int
    file: str
    line: int | None
    severity: str
    title: str

    def render(self) -> str:
        where = f"{self.file}:{self.line}" if self.line else self.file
        return f"[{self.ordinal}] {self.severity} · {where} — {self.title}"


def strip_mention(body: str, handle: str) -> str:
    """The comment with the trigger token removed."""
    return re.sub(re.escape(handle), " ", body, flags=re.IGNORECASE).strip()


def is_bare_mention(body: str, handle: str) -> bool:
    """Whether the comment is the trigger and nothing else.

    Punctuation and whitespace do not count as an instruction, so `@reviewhive?`
    and `@reviewhive !!` are both bare. Anything with a word in it is not.
    """
    remainder = strip_mention(body, handle)
    return not re.search(r"\w", remainder)


def default_intent(reason: str) -> CommentIntent:
    """A full re-review, for when there is nothing to interpret."""
    return CommentIntent(action="full_review", rationale=reason)


async def classify(
    client: AsyncAnthropic,
    settings: Settings,
    *,
    comment: str,
    findings: list[PriorFinding] | None = None,
    thread_target: int | None = None,
) -> tuple[CommentIntent, AgentCall | None]:
    """Read one comment. Returns the intent and the call it cost, if any.

    Never raises. A classifier failure falls back to `answer_question` rather than
    to a re-review: the cheapest action that cannot be wrong in an expensive
    direction. Losing the ability to read a comment is not a reason to spend three
    agent calls on it.
    """
    body = strip_mention(comment, settings.mention_handle)
    if not re.search(r"\w", body):
        return default_intent("bare mention, nothing to interpret"), None

    started = time.perf_counter()
    call = AgentCall(
        agent="intent", model=settings.agent_model, input_tokens=0, output_tokens=0
    )

    try:
        message = await client.messages.parse(
            model=settings.agent_model,
            max_tokens=settings.agent_max_tokens,
            system=load_prompt(PROMPT_FILE, shared=False),
            messages=[{"role": "user", "content": _user_message(body, findings, thread_target)}],
            output_format=CommentIntent,
        )
    except APIError as exc:
        logger.warning("could not classify a mention: %s", exc)
        call.error = f"{type(exc).__name__}: {exc}"
        call.latency_ms = _elapsed_ms(started)
        return _unreadable(body), call

    call.latency_ms = _elapsed_ms(started)
    call.input_tokens = message.usage.input_tokens
    call.output_tokens = message.usage.output_tokens
    call.cache_read_tokens = message.usage.cache_read_input_tokens or 0

    intent = message.parsed_output
    if intent is None or message.stop_reason == "refusal":
        call.error = f"unusable output (stop_reason={message.stop_reason})"
        logger.warning("classifier returned nothing usable")
        return _unreadable(body), call

    # The payload knows better than the model does. A reply inside a thread names
    # the comment it answers, so the finding is already determined; letting a
    # guess override it would be choosing the weaker source.
    if thread_target is not None:
        intent = intent.model_copy(update={"target_ordinal": thread_target})

    logger.info("mention read as %s: %s", intent.action, intent.rationale)
    return intent, call


def _unreadable(body: str) -> CommentIntent:
    return CommentIntent(
        action="answer_question",
        question=body,
        rationale="could not classify the comment; answering it as asked",
    )


def _user_message(
    body: str, findings: list[PriorFinding] | None, thread_target: int | None
) -> str:
    parts = [f"<comment>\n{body}\n</comment>"]

    if findings:
        listed = "\n".join(f.render() for f in findings)
        parts.append(
            "Findings already posted on this pull request:\n"
            f"<findings>\n{listed}\n</findings>"
        )
    else:
        parts.append("No findings have been posted on this pull request yet.")

    if thread_target is not None:
        parts.append(
            f"This comment is a reply within the thread on finding [{thread_target}]. "
            f"That finding is already determined; do not choose a different one."
        )

    return "\n\n".join(parts)


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
