"""An in-memory stand-in for `AsyncAnthropic`.

The graph takes its client as a constructor argument, so tests hand it one of these
and need no monkeypatching. It records every call, can simulate latency (which is
how `test_graph.py` proves the fan-out is genuinely concurrent), and can be told to
fail or refuse for a given agent.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace

import httpx
from anthropic import APIStatusError

from reviewhive.models import AgentFindings, Finding

AGENT_MARKERS = {
    "security and correctness": "security",
    "style, readability": "style",
    "structure, abstraction": "architecture",
}


def identify_agent(system_prompt: str) -> str:
    """Work out which agent is calling from its specialty section."""
    lowered = system_prompt.lower()
    for marker, name in AGENT_MARKERS.items():
        if marker in lowered:
            return name
    return "unknown"


@dataclass
class StubAnthropic:
    """Quacks like `AsyncAnthropic` for the two methods the app uses."""

    responses: dict[str, list[Finding]] = field(default_factory=dict)
    # Canned results for schemas other than `AgentFindings`, keyed by the output
    # model's class name. The mention paths ask for their own shapes, and a stub
    # that only knows how to be a reviewer cannot stand in for them.
    outputs: dict[str, object] = field(default_factory=dict)
    latency: float = 0.0
    tokens_per_call: int = 100
    errors: dict[str, Exception] = field(default_factory=dict)
    refusals: set[str] = field(default_factory=set)
    # A response the SDK could not validate against the schema. Distinct from a
    # refusal: the model answered, and what came back is unusable.
    unparsed: set[str] = field(default_factory=set)
    # Any other stop reason, keyed the same way. `max_tokens` is the one that
    # matters — structured output truncated at the cap still parses, so the caller
    # sees a short list rather than an error unless it checks this.
    stop_reasons: dict[str, str] = field(default_factory=dict)

    parse_calls: list[str] = field(default_factory=list)
    # Everything passed to `parse` beyond system/messages/output_format. Sampling
    # parameters reach the API and nothing else observes them, so without this a
    # setting could stop being sent and every test would still pass.
    parse_kwargs: list[dict] = field(default_factory=list)
    # The user turn each agent was sent. Recorded because the system prompt is
    # identical on every run, so anything per-request — the diff, a focus — can
    # only be asserted here.
    user_messages: list[str] = field(default_factory=list)
    count_calls: list[str] = field(default_factory=list)
    max_concurrent: int = 0
    _in_flight: int = 0

    def __post_init__(self) -> None:
        self.messages = SimpleNamespace(parse=self._parse, count_tokens=self._count_tokens)

    async def _parse(self, *, system: str, messages=None, output_format=None, **_kwargs):
        if messages:
            self.user_messages.append(messages[0]["content"])
        self.parse_kwargs.append(dict(_kwargs))

        schema = getattr(output_format, "__name__", "AgentFindings")
        if schema != "AgentFindings":
            return await self._parse_other(schema)

        agent = identify_agent(system)
        self.parse_calls.append(agent)

        self._in_flight += 1
        self.max_concurrent = max(self.max_concurrent, self._in_flight)
        try:
            if self.latency:
                await asyncio.sleep(self.latency)
            if agent in self.errors:
                raise self.errors[agent]

            findings = self.responses.get(agent, [])
            return SimpleNamespace(
                parsed_output=None if agent in self.unparsed else AgentFindings(findings=findings),
                stop_reason=self._stop_reason(agent),
                usage=SimpleNamespace(
                    input_tokens=self.tokens_per_call,
                    output_tokens=len(findings) * 50,
                    cache_read_input_tokens=0,
                ),
            )
        finally:
            self._in_flight -= 1

    async def _parse_other(self, schema: str):
        """A call asking for something other than findings.

        Recorded under the schema name rather than an agent name: these have no
        specialty to identify, and the system prompt they carry is not a
        reviewer's.
        """
        self.parse_calls.append(schema)
        if self.latency:
            await asyncio.sleep(self.latency)
        if schema in self.errors:
            raise self.errors[schema]

        return SimpleNamespace(
            parsed_output=self.outputs.get(schema),
            stop_reason=self._stop_reason(schema),
            usage=SimpleNamespace(
                input_tokens=self.tokens_per_call,
                output_tokens=40,
                cache_read_input_tokens=0,
            ),
        )

    def _stop_reason(self, key: str) -> str:
        """A refusal outranks an explicit stop reason: it is the API's own verdict
        on the request, not something a caller chose to simulate."""
        if key in self.refusals:
            return "refusal"
        return self.stop_reasons.get(key, "end_turn")

    async def _count_tokens(self, *, messages, **_kwargs):
        text = messages[0]["content"]
        self.count_calls.append(text)
        return SimpleNamespace(input_tokens=max(1, len(text) // 4))


def overloaded_error() -> APIStatusError:
    """A realistic 529 from the API.

    `APIStatusError` reads `response.request`, so it cannot be built from None —
    constructing it properly here keeps that detail out of every test.
    """
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return APIStatusError(
        "Overloaded",
        response=httpx.Response(529, request=request),
        body={"type": "error", "error": {"type": "overloaded_error"}},
    )


def finding(
    *,
    file: str = "src/app/auth.py",
    line: int | None = 13,
    severity: str = "high",
    category: str = "sql-injection",
    title: str = "SQL query built by string concatenation",
    body: str = "Use a parameterised query instead.",
    confidence: float = 0.9,
) -> Finding:
    """Terse Finding builder so tests state only what they care about."""
    return Finding(
        file=file,
        line=line,
        severity=severity,  # type: ignore[arg-type]
        category=category,
        title=title,
        body=body,
        confidence=confidence,
    )
