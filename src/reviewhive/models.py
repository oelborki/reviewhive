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


class Finding(BaseModel):
    """One issue reported by one agent."""

    file: str = Field(description="Repo-relative path of the file the finding is in.")
    line: int | None = Field(
        default=None,
        description=(
            "1-indexed line in the file's new version that the finding anchors to. "
            "Null for a finding about the file as a whole."
        ),
    )
    severity: Severity = Field(description="How much this matters if left unfixed.")
    category: str = Field(
        description="Short kebab-case slug for the kind of issue, e.g. 'sql-injection'."
    )
    title: str = Field(
        description="The claim alone, under 80 characters. No rationale, no consequence clause."
    )
    body: str = Field(
        description="Why it is a problem and what to do instead. Two to four sentences."
    )
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
        description="Agents that independently raised this. Length > 1 means agreement."
    )

    @property
    def agreement(self) -> int:
        return len(self.sources)

    @classmethod
    def from_finding(cls, finding: Finding, source: AgentName) -> MergedFinding:
        return cls(**finding.model_dump(), sources=[source])


class AgentCall(BaseModel):
    """Telemetry for one Anthropic request. Persisted in Phase 2, logged in Phase 1."""

    agent: AgentName
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    latency_ms: int = 0
    findings_returned: int = 0
    error: str | None = None

    def cost_usd(self, input_per_mtok: float, output_per_mtok: float) -> float:
        return (
            self.input_tokens * input_per_mtok + self.output_tokens * output_per_mtok
        ) / 1_000_000


class ReviewResult(BaseModel):
    """Everything one review run produced. The graph's terminal output."""

    findings: list[MergedFinding] = Field(default_factory=list)
    suppressed_count: int = Field(
        default=0, description="Findings that survived dedup but were cut by the posting cap."
    )
    skipped_files: list[str] = Field(
        default_factory=list, description="Files excluded from review, with the reason appended."
    )
    truncated_files: list[str] = Field(default_factory=list)
    calls: list[AgentCall] = Field(default_factory=list)

    @property
    def total_cost_usd(self) -> float:
        # Haiku 4.5 list price. Phase 2 moves this into config alongside the model id.
        return sum(c.cost_usd(1.00, 5.00) for c in self.calls)
