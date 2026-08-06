"""Domain models.

`Finding` doubles as the structured-output schema sent to the Anthropic API, so it
is kept deliberately flat: only types the API's JSON-schema subset supports (basic
types, enums, and nullable via anyOf). Range/length constraints are omitted on
purpose — the SDK strips them from the wire schema and enforces them client-side,
which would turn a slightly-out-of-range confidence into a hard validation error
and lose the entire agent's output. We clamp instead.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Severity = Literal["high", "medium", "low"]

SEVERITY_RANK: dict[Severity, int] = {"high": 3, "medium": 2, "low": 1}

AgentName = Literal["security", "style", "architecture"]

# Everything that can spend tokens on a pull request, which is wider than the set
# of reviewers. Kept separate from `AgentName` on purpose: `AgentName` also types
# `MergedFinding.sources`, and a classifier must never be attributable as the
# source of a finding. Cost telemetry is a different question from authorship.
CallAgent = Literal[
    "security", "style", "architecture", "intent", "answer", "reconsider", "merge"
]


class Finding(BaseModel):
    """One issue reported by one agent.

    **Field order is behaviour, not style. Do not reorder these.** This schema is
    what the model fills in, and generation is autoregressive: a field is written
    with only the fields above it in context. `severity` used to sit third, so it
    was committed while nothing but `file` and `line` existed, and the body written
    afterwards rationalised a severity that was already chosen rather than
    informing it. That produced a finding whose own body read "`json.dumps(...)`
    already does this" filed at high severity, and a "bypass" filed at high against
    a check that fails closed.

    `confidence` never had the problem because it was always last. That asymmetry —
    same call, same model, different position — is the evidence, and it is why the
    judgement fields now all follow the evidence they judge.
    """

    # Where. Cheap to emit and it orients everything below it.
    file: str = Field(description="Repo-relative path of the file the finding is in.")
    line: int | None = Field(
        default=None,
        description=(
            "1-indexed line in the file's new version that the finding anchors to. "
            "Null for a finding about the file as a whole."
        ),
    )
    # The evidence. Written before anything that grades it.
    title: str = Field(
        description="The claim alone, under 80 characters. No rationale, no consequence clause."
    )
    body: str = Field(
        description="Why it is a problem and what to do instead. Two to four sentences."
    )
    # The judgements, each conditioned on the evidence above.
    category: str = Field(
        description="Short kebab-case slug for the kind of issue, e.g. 'sql-injection'."
    )
    severity: Severity = Field(description="How much this matters if left unfixed.")
    confidence: float = Field(
        description="0.0 to 1.0. How sure you are this is a real issue and not a false positive."
    )

    @field_validator("confidence", mode="after")
    @classmethod
    def _clamp_confidence(cls, v: float) -> float:
        return min(1.0, max(0.0, v))

    @field_validator("line", mode="after")
    @classmethod
    def _reject_nonpositive_line(cls, v: int | None) -> int | None:
        # A model occasionally emits 0 or a negative line for a file-level remark.
        # Treat that as "no anchor" rather than failing the parse.
        return v if v is not None and v > 0 else None


class AgentFindings(BaseModel):
    """Top-level structured-output schema for a single agent call."""

    findings: list[Finding] = Field(
        description="Every issue found. Empty list if the diff looks fine."
    )


class MergedFinding(BaseModel):
    """A finding after deduplication, carrying which agents raised it."""

    file: str
    line: int | None
    severity: Severity
    category: str
    title: str
    body: str
    confidence: float
    sources: list[AgentName] = Field(
        description=(
            "Agents that raised this. Length > 1 records who, not that the reports "
            "were independent — the reviewers see the same diff and converge."
        )
    )

    @property
    def agreement(self) -> int:
        return len(self.sources)

    @classmethod
    def from_finding(cls, finding: Finding, source: AgentName) -> MergedFinding:
        return cls(**finding.model_dump(), sources=[source])


class AgentCall(BaseModel):
    """Telemetry for one Anthropic request.

    Carries no identity, timestamp, or ordering key on purpose — the graph should
    not be minting ids or reading clocks. The database supplies all three when the
    call is persisted. What a call *cost* lives in `reviewhive.pricing`, so the
    domain types stay free of a price list.
    """

    agent: CallAgent
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    latency_ms: int = 0
    findings_returned: int = 0
    error: str | None = None


class ReviewResult(BaseModel):
    """Everything one review run produced. The graph's terminal output.

    `findings` holds only what was *posted*: `rank_and_cut` drops anything below
    the confidence floor and anything past the cap, and only `suppressed_count`
    survives of the rest. Anything persisting this stores posted findings, not all
    of them.

    For the run's cost, call `reviewhive.pricing.total_cost` — it is not a member
    here because that would put a price list inside the domain model.
    """

    findings: list[MergedFinding] = Field(default_factory=list)
    suppressed_count: int = Field(
        default=0, description="Findings that survived dedup but were cut by the posting cap."
    )
    skipped_files: list[str] = Field(
        default_factory=list, description="Files excluded from review, with the reason appended."
    )
    truncated_files: list[str] = Field(default_factory=list)
    calls: list[AgentCall] = Field(default_factory=list)
    focus: str | None = Field(
        default=None,
        description=(
            "What the run was narrowed to, when a reviewer asked for a narrower "
            "second look. Carried on the result rather than held by the caller "
            "because a narrowed review is not comparable to a full one, and "
            "anything reading these afterwards needs to know which it has."
        ),
    )
